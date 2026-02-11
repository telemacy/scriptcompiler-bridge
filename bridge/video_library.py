import os
import mimetypes
import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import StreamingResponse, Response

from .config import VIDEO_EXTENSIONS
from .settings import get_video_folders

logger = logging.getLogger(__name__)

MIME_MAP = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".m4v": "video/x-m4v",
}

CHUNK_SIZE = 64 * 1024  # 64KB

_cached_videos = None


def is_path_in_allowed_folders(file_path, folders):
    real_path = os.path.realpath(file_path)
    for folder in folders:
        real_folder = os.path.realpath(folder)
        if real_path.startswith(real_folder + os.sep) or real_path == real_folder:
            return True
    return False


def scan_video_folders(folders=None):
    if folders is None:
        folders = get_video_folders()

    videos = []
    ext_set = set("." + ext.lower() for ext in VIDEO_EXTENSIONS)

    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in ext_set:
                    continue

                full_path = os.path.join(root, fname)
                try:
                    stat = os.stat(full_path)
                except OSError:
                    continue

                base = os.path.splitext(full_path)[0]
                funscript_path = base + ".funscript"
                has_funscript = os.path.isfile(funscript_path)

                videos.append({
                    "filename": fname,
                    "path": full_path,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "extension": ext.lstrip("."),
                    "has_funscript": has_funscript,
                    "funscript_path": funscript_path if has_funscript else None,
                    "folder": folder,
                })

    videos.sort(key=lambda v: v["modified"], reverse=True)
    return videos


def scan_and_cache():
    global _cached_videos
    _cached_videos = scan_video_folders()
    return _cached_videos


def get_cached_videos():
    global _cached_videos
    if _cached_videos is None:
        return scan_and_cache()
    return _cached_videos


def invalidate_cache():
    global _cached_videos
    _cached_videos = None


def get_mime_type(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    return MIME_MAP.get(ext, "application/octet-stream")


async def stream_video(file_path: str, request: Request):
    if not os.path.isfile(file_path):
        return Response(status_code=404, content="File not found")

    folders = get_video_folders()
    if not is_path_in_allowed_folders(file_path, folders):
        return Response(status_code=403, content="Access denied")

    file_size = os.path.getsize(file_path)
    mime_type = get_mime_type(file_path)
    range_header = request.headers.get("range")

    if range_header:
        range_spec = range_header.strip().replace("bytes=", "")
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def iter_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Cache-Control": "no-cache",
            },
        )
    else:
        def iter_full():
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            iter_full(),
            status_code=200,
            media_type=mime_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Cache-Control": "no-cache",
            },
        )
