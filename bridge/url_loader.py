import asyncio
import os
import re
import uuid
import logging

from .settings import get_settings, get_video_folders
from .ytdlp_utils import get_ytdlp_path

logger = logging.getLogger(__name__)

# Active downloads: download_id -> {process, file_path, url}
_active_downloads = {}

# Map url -> download_id for deduplication
_url_to_download_id = {}

_PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+[\d.]+\S+\s+at\s+(\S+)\s+ETA\s+(\S+)"
)


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


async def get_output_filename(url: str, output_folder: str) -> str:
    """Ask yt-dlp what the output filename will be without downloading."""
    ytdlp = get_ytdlp_path()
    quality = _get_quality()
    template = os.path.join(output_folder, "%(title)s.%(ext)s")

    proc = await asyncio.create_subprocess_exec(
        ytdlp,
        "--get-filename",
        "-f", quality,
        "-o", template,
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


async def start_download(url: str, websocket_broadcast) -> tuple:
    """
    Start downloading url. Returns (download_id, file_path).
    Responds immediately; download continues in background.
    websocket_broadcast: async callable(dict) to send progress events.
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

    file_path = await get_output_filename(url, output_folder)
    download_id = str(uuid.uuid4())[:8]

    ytdlp = get_ytdlp_path()
    quality = _get_quality()
    template = os.path.join(output_folder, "%(title)s.%(ext)s")

    proc = await asyncio.create_subprocess_exec(
        ytdlp,
        "-f", quality,
        "-o", template,
        "--newline",
        "--progress",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    _active_downloads[download_id] = {"process": proc, "file_path": file_path, "url": url, "cancelled": False}
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
    elif not was_cancelled:
        await broadcast({
            "type": "download_error",
            "download_id": download_id,
            "message": "Download failed",
        })


def cancel_download(download_id: str) -> bool:
    """Kill the yt-dlp process for a given download_id. Returns True if found."""
    entry = _active_downloads.get(download_id)
    if not entry:
        return False
    entry["cancelled"] = True
    _url_to_download_id.pop(entry["url"], None)
    try:
        entry["process"].terminate()
    except Exception:
        pass
    return True
