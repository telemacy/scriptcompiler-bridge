import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
import logging

from .settings import get_settings, get_video_folders
from .ytdlp_utils import get_ytdlp_path
from .video_library import scan_and_cache as _scan_and_cache

logger = logging.getLogger(__name__)

# Active downloads: download_id -> {process, file_path, url}
_active_downloads = {}

# Map url -> download_id for deduplication
_url_to_download_id = {}

_PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*[\d.]+\S+\s+at\s+(.+?)\s+ETA\s+(\S+)"
)


async def _broadcast_library_updated(broadcast):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _scan_and_cache)
        await broadcast({"type": "library_updated"})
    except Exception as e:
        logger.warning("Failed to broadcast library_updated: %s", e)


def _get_output_folder():
    folders = get_video_folders()
    if not folders:
        return None
    return folders[0]


def _get_quality():
    return get_settings().get(
        "yt_dlp_quality",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    )


async def get_output_filename(url: str, output_folder: str, output_template: str = None) -> str:
    """Ask yt-dlp what the output filename will be without downloading."""
    ytdlp = get_ytdlp_path()
    if ytdlp is None:
        raise ValueError("yt-dlp binary not found. Please reinstall the bridge.")
    quality = _get_quality()
    if output_template is None:
        output_template = os.path.join(output_folder, "%(title)s.%(ext)s")

    proc = await asyncio.create_subprocess_exec(
        ytdlp,
        "--get-filename",
        "-f", quality,
        "-o", output_template,
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"yt-dlp error: {error_msg}")

    lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not lines or not lines[0]:
        raise ValueError("yt-dlp returned no filename")
    filename = lines[0]
    return filename


async def fetch_video_info(url: str) -> dict:
    ytdlp = get_ytdlp_path()
    if ytdlp is None:
        raise ValueError("yt-dlp binary not found. Please reinstall the bridge.")

    proc = await asyncio.create_subprocess_exec(
        ytdlp,
        "--dump-json",
        "--no-playlist",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError("Timed out fetching video info (30s)")

    if proc.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        lines = [l for l in error_msg.splitlines() if l.strip()]
        raise ValueError(lines[-1] if lines else "yt-dlp returned an error")

    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        raise ValueError("yt-dlp returned no output")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse yt-dlp output: {e}")

    cast = info.get("cast") or []
    if not cast and info.get("artists"):
        artists = info["artists"]
        cast = artists if isinstance(artists, list) else [artists]

    tags = info.get("tags") or []
    categories = info.get("categories") or []
    all_tags = list(dict.fromkeys(tags + categories))

    return {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or "",
        "cast": cast,
        "tags": all_tags,
        "webpage_url": info.get("webpage_url") or url,
    }


async def start_download(url: str, websocket_broadcast, video_info=None) -> tuple:
    """
    Start downloading url. Returns (download_id, file_path).
    Responds immediately; download continues in background.
    websocket_broadcast: async callable(dict) to send progress events.
    video_info: optional dict with title, thumbnail, uploader, duration, cast, tags.
    """
    # Dedup: return existing download for same URL
    if url in _url_to_download_id:
        existing_id = _url_to_download_id[url]
        if existing_id in _active_downloads:
            entry = _active_downloads[existing_id]
            return existing_id, entry["file_path"]

    output_folder = _get_output_folder()
    if not output_folder:
        raise ValueError("NO_VIDEO_FOLDER")

    download_id = str(uuid.uuid4())[:8]
    template = os.path.join(output_folder, f"%(title)s [{download_id}].%(ext)s")

    file_path = await get_output_filename(url, output_folder, output_template=template)

    ytdlp = get_ytdlp_path()
    if ytdlp is None:
        raise ValueError("yt-dlp binary not found. Please reinstall the bridge.")
    quality = _get_quality()

    # On Windows, create a new process group so we can kill the entire tree
    # (yt-dlp may spawn ffmpeg child processes that outlive the parent)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = await asyncio.create_subprocess_exec(
        ytdlp,
        "-f", quality,
        "-o", template,
        "--newline",
        "--progress",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )

    _active_downloads[download_id] = {
        "process": proc, "file_path": file_path, "url": url,
        "cancelled": False, "video_info": video_info,
    }
    _url_to_download_id[url] = download_id

    asyncio.create_task(_monitor_progress(download_id, proc, file_path, websocket_broadcast))

    return download_id, file_path


async def _monitor_progress(download_id: str, proc, file_path: str, broadcast):
    """Read yt-dlp stdout and broadcast progress events."""
    try:
        async for line_bytes in proc.stdout:
            line = line_bytes.decode("utf-8", errors="replace").strip()
            m = _PROGRESS_RE.search(line)
            if m:
                await broadcast({
                    "type": "download_progress",
                    "download_id": download_id,
                    "percent": float(m.group(1)),
                    "speed": m.group(2),
                    "eta": m.group(3),
                })
    except Exception as e:
        logger.warning("Error reading yt-dlp output for %s: %s", download_id, e)

    await proc.wait()

    entry = _active_downloads.pop(download_id, None)
    was_cancelled = entry["cancelled"] if entry else False
    if entry:
        _url_to_download_id.pop(entry["url"], None)

    if proc.returncode == 0:
        await broadcast({
            "type": "download_complete",
            "download_id": download_id,
            "file_path": file_path,
        })
        asyncio.create_task(_broadcast_library_updated(broadcast))
    elif was_cancelled:
        await broadcast({
            "type": "download_cancelled",
            "download_id": download_id,
        })
    else:
        await broadcast({
            "type": "download_error",
            "download_id": download_id,
            "message": "Download failed",
        })


def get_active_downloads() -> list:
    """Return list of currently active downloads."""
    result = []
    for did, entry in _active_downloads.items():
        if entry.get("cancelled"):
            continue
        result.append({
            "download_id": did,
            "file_path": entry["file_path"],
            "url": entry["url"],
            "video_info": entry.get("video_info"),
        })
    return result


def cancel_download(download_id: str) -> bool:
    """Kill the yt-dlp process for a given download_id. Returns True if found."""
    entry = _active_downloads.get(download_id)
    if not entry:
        return False
    entry["cancelled"] = True
    _url_to_download_id.pop(entry["url"], None)
    proc = entry["process"]
    try:
        if sys.platform == "win32":
            # Kill the entire process tree on Windows (yt-dlp + ffmpeg children)
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc.kill()
    except Exception:
        pass
    return True
