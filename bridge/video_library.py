import os
import mimetypes
import logging
import threading
import time as _time
from pathlib import Path

from fastapi import Request
from fastapi.responses import StreamingResponse, Response

from .config import VIDEO_EXTENSIONS
from .settings import get_video_folders

logger = logging.getLogger(__name__)


class _CachedCapture:
    """Keeps a cv2.VideoCapture handle open for reuse across frame requests.
    Auto-closes after idle_timeout seconds of inactivity.
    Thread-safe: the lock is held for the entire seek+read+encode cycle.
    """

    def __init__(self, idle_timeout=30.0):
        self._lock = threading.Lock()
        self._cap = None
        self._path = None
        self._last_used = 0.0
        self._idle_timeout = idle_timeout
        self._timer = None

    def _schedule_cleanup(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle_timeout, self._cleanup_if_idle)
        self._timer.daemon = True
        self._timer.start()

    def _cleanup_if_idle(self):
        with self._lock:
            if self._cap is not None and (_time.monotonic() - self._last_used) >= self._idle_timeout:
                self._cap.release()
                self._cap = None
                self._path = None

    def _ensure_cap(self, file_path):
        import cv2

        if self._cap is not None and self._path == file_path:
            self._last_used = _time.monotonic()
            self._schedule_cleanup()
            return self._cap

        if self._cap is not None:
            self._cap.release()
            self._cap = None
            self._path = None

        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            return None

        self._cap = cap
        self._path = file_path
        self._last_used = _time.monotonic()
        self._schedule_cleanup()
        return self._cap

    def extract_frame(self, file_path, time_ms):
        import cv2

        with self._lock:
            cap = self._ensure_cap(file_path)
            if cap is None:
                return None

            cap.set(cv2.CAP_PROP_POS_MSEC, time_ms)
            ret, frame = cap.read()
            if not ret:
                return None

            h, w = frame.shape[:2]
            thumb_w = min(w, 320)
            thumb_h = int(h * (thumb_w / w))
            frame = cv2.resize(frame, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)

            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return buf.tobytes()

    def extract_frames_batch(self, file_path, times_ms):
        import cv2

        with self._lock:
            t_batch_start = _time.perf_counter()
            cap = self._ensure_cap(file_path)
            if cap is None:
                return {}

            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            # If next target is within this many ms ahead of current position,
            # read forward instead of seeking. ~2 seconds worth of frames.
            read_ahead_threshold = 2000.0

            results = {}
            current_pos = -1.0

            for t in sorted(times_ms):
                # Decide: seek or read forward?
                if current_pos < 0 or t < current_pos or (t - current_pos) > read_ahead_threshold:
                    # Must seek: first frame, backwards, or too far ahead
                    cap.set(cv2.CAP_PROP_POS_MSEC, t)
                else:
                    # Read forward to target - much faster than seeking
                    frames_to_skip = int((t - current_pos) / (1000.0 / fps)) - 1
                    for _ in range(max(0, frames_to_skip)):
                        cap.grab()

                ret, frame = cap.read()
                if not ret:
                    continue

                current_pos = cap.get(cv2.CAP_PROP_POS_MSEC)

                h, w = frame.shape[:2]
                thumb_w = min(w, 320)
                thumb_h = int(h * (thumb_w / w))
                frame = cv2.resize(frame, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)

                _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                results[t] = buf.tobytes()

            t_batch_end = _time.perf_counter()
            count = len(results)
            total_ms = (t_batch_end - t_batch_start) * 1000
            avg = total_ms / count if count > 0 else 0
            logger.info("batch: %d frames in %.1fms (%.1fms/frame avg)", count, total_ms, avg)

            return results

    def release(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._cap is not None:
                self._cap.release()
                self._cap = None
                self._path = None


_frame_capture = _CachedCapture(idle_timeout=30.0)

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


def generate_frame_at_time(file_path: str, time_ms: float):
    """Extract a single JPEG frame from a video at a specific millisecond timestamp."""
    return _frame_capture.extract_frame(file_path, time_ms)


def generate_frames_batch(file_path: str, times_ms: list):
    """Extract multiple JPEG frames from a video at specific millisecond timestamps.
    Returns a dict mapping time_ms -> jpeg_bytes.
    """
    return _frame_capture.extract_frames_batch(file_path, times_ms)


def generate_thumbnail(file_path: str, seek_percent: float = 10.0):
    """Generate a JPEG thumbnail from a video file using OpenCV."""
    try:
        import cv2
    except ImportError:
        return None

    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        return None

    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames > 0:
            target_frame = int(total_frames * (seek_percent / 100.0))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

        ret, frame = cap.read()
        if not ret:
            return None

        # Resize to thumbnail size (max width 320, maintain aspect ratio)
        h, w = frame.shape[:2]
        thumb_w = min(w, 320)
        thumb_h = int(h * (thumb_w / w))
        frame = cv2.resize(frame, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)

        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes()
    finally:
        cap.release()


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
