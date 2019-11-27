# Photo module for pyTivo by William McBrine <wmcbrine@users.sf.net>
# based partly on music.py and plugin.py
#
# After version 0.15, see git for the history
#
# Version 0.15, Dec. 29 -- allow Unicode; better error messages
# Version 0.14, Dec. 26 -- fix Random sort; handle ItemCount == 0
# Version 0.13, Dec. 19 -- more thread-safe; use draft mode always
# Version 0.12, Dec. 18 -- get date and orientation from Exif
# Version 0.11, Dec. 16 -- handle ItemCount, AnchorItem etc. correctly
# Version 0.10, Dec. 14 -- give full list if no ItemCount; use antialias
#                          mode always; allow larger thumbnails
# Version 0.9,  Dec. 13 -- different sort types
# Version 0.8,  Dec. 12 -- faster thumbnails, better quality full views
# Version 0.7,  Dec. 11 -- fix missing item on thumbnail scroll up,
#                          better anchor and path handling
# Version 0.6,  Dec. 10 -- cache recursive lookups for faster slide shows
# Version 0.5,  Dec. 10 -- fix reboot problem by keeping directory names
#                          (vs. contents) out of "Recurse=Yes" lists
# Version 0.4,  Dec. 10 -- drop the use of playable_cache, add path
#                          separator kludges for Windows
# Version 0.3,  Dec. 8  -- revert to using PixelShape, workaround for
#                          Image.save() under Windows
# Version 0.2,  Dec. 8  -- thumbnail caching, faster thumbnails
# Version 0.1,  Dec. 7, 2007

from functools import partial
import os
import re
import random
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Optional, List, Any, Dict, Callable, Tuple, Union
import unicodedata
import urllib.request, urllib.parse, urllib.error
from io import BytesIO
from xml.sax.saxutils import escape

use_pil = True
try:
    from PIL import Image  # type: ignore
except ImportError:
    try:
        # TODO 20191125: what the heck is this library?
        import Image  # type: ignore
    except ImportError:
        use_pil = False
        print("Python Imaging Library not found; using FFmpeg")

from Cheetah.Template import Template  # type: ignore

import config
from lrucache import LRUCache
from plugin import Plugin, quote, unquote, build_recursive_list, SortList, read_tmpl
from plugins.video.transcode import kill
from pytivo_types import Query, FileData

if TYPE_CHECKING:
    from httpserver import TivoHTTPHandler

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = "Photo"

IMAGE_FILE_EXTS = (
    ".jpg",
    ".gif",
    ".png",
    ".bmp",
    ".tif",
    ".xbm",
    ".xpm",
    ".pgm",
    ".pbm",
    ".ppm",
    ".pcx",
    ".tga",
    ".fpx",
    ".ico",
    ".pcd",
    ".jpeg",
    ".tiff",
    ".nef",
)

# Match Exif date -- YYYY:MM:DD HH:MM:SS
exif_date = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)").search

# Match Exif orientation, Intel and Motorola versions
exif_orient_i = re.compile("\x12\x01\x03\x00\x01\x00\x00\x00(.)\x00\x00\x00").search
exif_orient_m = re.compile("\x01\x12\x00\x03\x00\x00\x00\x01\x00(.)\x00\x00").search

# Find size in FFmpeg output
ffmpeg_size = re.compile(r".*Video: .+, (\d+)x(\d+)[, ].*")

# Preload the template
PHOTO_TEMPLATE = read_tmpl(os.path.join(SCRIPTDIR, "templates", "container.tmpl"))
ITEM_TEMPLATE = read_tmpl(os.path.join(SCRIPTDIR, "templates", "item.tmpl"))

JFIF_TAG = b"\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"


def ImageFileFilter(f: str, filter_type: Optional[str] = None) -> bool:
    return os.path.splitext(f)[1].lower() in IMAGE_FILE_EXTS


def send_jpeg(handler: "TivoHTTPHandler", data: bytes) -> None:
    handler.send_fixed(data, "image/jpeg")


class SortListLock(SortList):
    def __init__(self, files: List[FileData]) -> None:
        super().__init__(files)
        self.lock = threading.RLock()

    def acquire(self, blocking: bool = True) -> bool:
        return self.lock.acquire(blocking)

    def release(self) -> None:
        self.lock.release()


class LockedLRUCache(LRUCache):
    def __init__(self, num: int) -> None:
        LRUCache.__init__(self, num)
        self.lock = threading.RLock()

    def acquire(self, blocking: bool = True) -> bool:
        return self.lock.acquire(blocking)

    def release(self) -> None:
        self.lock.release()

    def __setitem__(self, key: str, obj: Any):
        self.acquire()
        try:
            LRUCache.__setitem__(self, key, obj)
        finally:
            self.release()

    def __getitem__(self, key: str) -> Any:
        item = None
        self.acquire()
        try:
            item = LRUCache.__getitem__(self, key)
        finally:
            self.release()
        return item


class Photo(Plugin):

    CONTENT_TYPE = "x-container/tivo-photos"

    media_data_cache = LockedLRUCache(300)  # info and thumbnails
    recurse_cache = LockedLRUCache(5)  # recursive directory lists
    dir_cache = LockedLRUCache(10)  # non-recursive lists

    def new_size(
        self, oldw: int, oldh: int, width: int, height: int, pshape: str
    ) -> Tuple[int, int]:
        pixw, pixh = [int(x) for x in pshape.split(":")]

        if not width:
            width = oldw
        if not height:
            height = oldh

        oldw *= pixh
        oldh *= pixw

        ratio = float(oldw) / oldh

        if float(width) / height < ratio:
            height = int(width / ratio)
        else:
            width = int(height * ratio)

        return width, height

    def parse_exif(self, exif, rot: int, attrs: Dict[str, Any]) -> int:
        # Capture date
        if attrs and not "odate" in attrs:
            date = exif_date(exif)
            if date:
                year, month, day, hour, minute, second = (int(x) for x in date.groups())
                if year:
                    odate = time.mktime(
                        (year, month, day, hour, minute, second, -1, -1, -1)
                    )
                    attrs["odate"] = "%#x" % int(odate)

        # Orientation
        if attrs and "exifrot" in attrs:
            rot = (rot + attrs["exifrot"]) % 360
        else:
            if exif[6] == "I":
                orient = exif_orient_i(exif)
            else:
                orient = exif_orient_m(exif)

            if orient:
                exifrot = {
                    1: 0,
                    2: 0,
                    3: 180,
                    4: 180,
                    5: 90,
                    6: -90,
                    7: -90,
                    8: 90,
                }.get(ord(orient.group(1)), 0)

                rot = (rot + exifrot) % 360
                if attrs:
                    attrs["exifrot"] = exifrot

        return rot

    def get_image_pil(
        self,
        path: str,
        width: int,
        height: int,
        pshape: str,
        rot: int,
        attrs: Dict[str, Any],
    ) -> Tuple[bool, bytes]:
        # Load
        try:
            pic = Image.open(path)
        except Exception as msg:
            return False, ("Could not open %s -- %s" % (path, msg)).encode()

        # Set draft mode
        try:
            pic.draft("RGB", (width, height))
        except Exception as msg:
            return (
                False,
                ("Failed to set draft mode for %s -- %s" % (path, msg)).encode(),
            )

        # Read Exif data if possible
        if "exif" in pic.info:
            rot = self.parse_exif(pic.info["exif"], rot, attrs)

        # Rotate
        try:
            if rot:
                pic = pic.rotate(rot)
        except Exception as msg:
            return False, ("Rotate failed on %s -- %s" % (path, msg)).encode()

        # De-palletize
        try:
            if pic.mode not in ("RGB", "L"):
                pic = pic.convert("RGB")
        except Exception as msg:
            return (
                False,
                ("Palette conversion failed on %s -- %s" % (path, msg)).encode(),
            )

        # Old size
        oldw, oldh = pic.size

        width, height = self.new_size(oldw, oldh, width, height, pshape)

        try:
            pic = pic.resize((width, height), Image.ANTIALIAS)
        except Exception as msg:
            return False, ("Resize failed on %s -- %s" % (path, msg)).encode()

        # Re-encode
        try:
            out = BytesIO()
            pic.save(out, "JPEG", quality=85)
            encoded = out.getvalue()
            out.close()
        except Exception as msg:
            return False, ("Encode failed on %s -- %s" % (path, msg)).encode()

        return True, encoded

    def get_size_ffmpeg(
        self, ffmpeg_path: str, fname: str
    ) -> Tuple[bool, Union[Tuple[int, int], bytes]]:
        cmd = [ffmpeg_path, "-i", fname]
        # Windows and other OS buffer 4096 and ffmpeg can output more
        # than that.
        err_tmp = tempfile.TemporaryFile()
        ffmpeg = subprocess.Popen(
            cmd, stderr=err_tmp, stdout=subprocess.PIPE, stdin=subprocess.PIPE
        )

        # wait configured # of seconds: if ffmpeg is not back give up
        limit = config.getFFmpegWait()
        if limit:
            for i in range(limit * 20):
                time.sleep(0.05)
                if not ffmpeg.poll() is None:
                    break

            if ffmpeg.poll() is None:
                kill(ffmpeg)
                return False, b"FFmpeg timed out"
        else:
            ffmpeg.wait()

        err_tmp.seek(0)
        output = err_tmp.read().decode()
        err_tmp.close()

        x = ffmpeg_size.search(output)
        if x:
            width = int(x.group(1))
            height = int(x.group(2))
        else:
            return False, b"Couldn't parse size"

        return True, (width, height)

    def get_image_ffmpeg(
        self,
        path: str,
        width: int,
        height: int,
        pshape: str,
        rot: int,
        attrs: Dict[str, Any],
    ) -> Tuple[bool, bytes]:
        ffmpeg_path = config.get_bin("ffmpeg")
        if not ffmpeg_path:
            return False, b"FFmpeg not found"

        if attrs and "size" in attrs:
            result = attrs["size"]
        else:
            status, result = self.get_size_ffmpeg(ffmpeg_path, path)
            if not status:
                return False, result
            if attrs:
                attrs["size"] = result

        if rot in (90, 270):
            oldh, oldw = result
        else:
            oldw, oldh = result

        width, height = self.new_size(oldw, oldh, width, height, pshape)

        if rot == 270:
            filters = "transpose=1,"
        elif rot == 180:
            filters = "hflip,vflip,"
        elif rot == 90:
            filters = "transpose=2,"
        else:
            filters = ""

        filters += "format=yuvj420p,"

        neww, newh = oldw, oldh
        while (neww / width >= 50) or (newh / height >= 50):
            neww /= 2
            newh /= 2
            filters += "scale=%d:%d," % (neww, newh)

        filters += "scale=%d:%d" % (width, height)

        cmd = [ffmpeg_path, "-i", path, "-vf", filters, "-f", "mjpeg", "-"]
        jpeg_tmp = tempfile.TemporaryFile()
        ffmpeg = subprocess.Popen(cmd, stdout=jpeg_tmp, stdin=subprocess.PIPE)

        # wait configured # of seconds: if ffmpeg is not back give up
        limit = config.getFFmpegWait()
        if limit:
            for i in range(limit * 20):
                time.sleep(0.05)
                if not ffmpeg.poll() is None:
                    break

            if ffmpeg.poll() is None:
                kill(ffmpeg)
                return False, b"FFmpeg timed out"
        else:
            ffmpeg.wait()

        jpeg_tmp.seek(0)
        output = jpeg_tmp.read()
        jpeg_tmp.close()

        if b"JFIF" not in output[:10]:
            output = output[:2] + JFIF_TAG + output[2:]

        return True, output

    def send_file(self, handler: "TivoHTTPHandler", path: str, query: Query) -> None:
        if "Format" in query and query["Format"][0] != "image/jpeg":
            handler.send_error(415)
            return

        try:
            attrs = self.media_data_cache[path]
        except:
            attrs = None

        # Set rotation
        if attrs:
            rot = attrs["rotation"]
        else:
            rot = 0

        if "Rotation" in query:
            rot = (rot - int(query["Rotation"][0])) % 360
            if attrs:
                attrs["rotation"] = rot
                if "thumb" in attrs:
                    del attrs["thumb"]

        # Requested size
        width = int(query.get("Width", ["0"])[0])
        height = int(query.get("Height", ["0"])[0])

        # Return saved thumbnail?
        if attrs and "thumb" in attrs and 0 < width < 100 and 0 < height < 100:
            send_jpeg(handler, attrs["thumb"])
            return

        # Requested pixel shape
        pshape = query.get("PixelShape", ["1:1"])[0]

        # Build a new image
        if use_pil:
            status, result = self.get_image_pil(path, width, height, pshape, rot, attrs)
        else:
            status, result = self.get_image_ffmpeg(
                path, width, height, pshape, rot, attrs
            )

        if status:
            # Save thumbnails
            if attrs and width < 100 and height < 100:
                attrs["thumb"] = result

            # Send it
            send_jpeg(handler, result)
        else:
            handler.server.logger.error(result)
            handler.send_error(404)

    def media_data(self, f: FileData, local_base_path: str) -> Dict[str, Any]:
        if f.name in self.media_data_cache:
            return self.media_data_cache[f.name]

        item: Dict[str, Any] = {}
        item["path"] = f.name
        item["part_path"] = f.name.replace(local_base_path, "", 1)
        item["name"] = os.path.basename(f.name)
        item["is_dir"] = f.isdir
        item["rotation"] = 0
        item["cdate"] = "%#x" % int(f.cdate)
        item["mdate"] = "%#x" % int(f.mdate)

        self.media_data_cache[f.name] = item
        return item

    def QueryContainer(self, handler: "TivoHTTPHandler", query: Query) -> None:
        # Reject a malformed request -- these attributes should only
        # appear in requests to send_file, but sometimes appear here
        badattrs = ("Rotation", "Width", "Height", "PixelShape")
        for i in badattrs:
            if i in query:
                handler.send_error(404)
                return

        local_base_path = self.get_local_base_path(handler, query)
        if not self.get_local_path(handler, query):
            handler.send_error(404)
            return

        t = Template(PHOTO_TEMPLATE)
        t.name = query["Container"][0]
        t.container = handler.cname
        t.files, t.total, t.start = self.get_files(handler, query, ImageFileFilter)
        t.files = list(
            map(partial(self.media_data, local_base_path=local_base_path), t.files)
        )
        t.quote = quote
        t.escape = escape

        handler.send_xml(str(t))

    def QueryItem(self, handler: "TivoHTTPHandler", query: Query) -> None:
        uq = urllib.parse.unquote_plus
        splitpath = [x for x in uq(query["Url"][0]).split("/") if x]
        path = os.path.join(handler.container["path"], *splitpath[1:])

        if path in self.media_data_cache:
            t = Template(ITEM_TEMPLATE)
            t.file = self.media_data_cache[path]
            t.escape = escape
            handler.send_xml(str(t))
        else:
            handler.send_error(404)

    def get_files(
        self,
        handler: "TivoHTTPHandler",
        query: Query,
        filterFunction: Optional[Callable] = None,
        force_alpha: bool = False,
        allow_recurse: bool = True,
    ) -> Tuple[List[Any], int, int]:
        path = self.get_local_path(handler, query)

        # Build the list
        recurse = query.get("Recurse", ["No"])[0] == "Yes"

        filelist = SortListLock([])
        rc = self.recurse_cache
        dc = self.dir_cache
        if recurse:
            if path in rc:
                filelist = rc[path]
        else:
            updated = os.path.getmtime(path)
            if path in dc and dc.mtime(path) >= updated:
                filelist = dc[path]
            for p in rc:
                if path.startswith(p) and rc.mtime(p) < updated:
                    del rc[p]

        if not filelist:
            filelist = SortListLock(
                build_recursive_list(path, recurse, filterFunction, "")
            )

            if recurse:
                rc[path] = filelist
            else:
                dc[path] = filelist

        filelist.acquire()

        # Sort it
        seed = ""
        start = ""
        sortby = query.get("SortOrder", ["Normal"])[0]
        if "Random" in sortby:
            if "RandomSeed" in query:
                seed = query["RandomSeed"][0]
                sortby += seed
            if "RandomStart" in query:
                start = query["RandomStart"][0]
                sortby += start

        if filelist.unsorted or filelist.sortby != sortby:
            if "Random" in sortby:
                self.random_lock.acquire()
                if seed:
                    random.seed(seed)
                random.shuffle(filelist.files)
                self.random_lock.release()
                if start:
                    local_base_path = self.get_local_base_path(handler, query)
                    start = unquote(start)
                    start = start.replace(
                        os.path.sep + handler.cname, local_base_path, 1
                    )
                    filenames = [x.name for x in filelist.files]
                    try:
                        index = filenames.index(start)
                        i = filelist.files.pop(index)
                        filelist.files.insert(0, i)
                    except ValueError:
                        handler.server.logger.warning("Start not found: " + start)
            else:
                if "Type" in sortby:
                    # secondary by ascending name
                    filelist.files.sort(key=lambda x: x.name)
                    # primary by descending isdir
                    filelist.files.sort(key=lambda x: x.isdir, reverse=True)
                elif "CaptureDate" in sortby:
                    filelist.files.sort(key=lambda x: x.cdate)
                elif "LastChangeDate" in sortby:
                    filelist.files.sort(key=lambda x: x.mdate)
                else:
                    filelist.files.sort(key=lambda x: x.name)

            filelist.sortby = sortby
            filelist.unsorted = False

        files = filelist.files[:]

        # Filter it -- this section needs work
        if "Filter" in query:
            usedir = "folder" in query["Filter"][0]
            useimg = "image" in query["Filter"][0]
            if not usedir:
                files = [x for x in files if not x.isdir]
            elif usedir and not useimg:
                files = [x for x in files if x.isdir]

        files, total, start_item = self.item_count(
            handler, query, handler.cname, files, filelist.last_start
        )
        filelist.last_start = start_item
        filelist.release()
        return files, total, start_item
