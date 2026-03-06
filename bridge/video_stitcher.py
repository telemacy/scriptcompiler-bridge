import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

_cancel_event = threading.Event()


def cancel_stitching():
    _cancel_event.set()


def _get_ffmpeg_path():
    """Return path to ffmpeg, checking bundled location first."""
    if getattr(sys, 'frozen', False):
        bundle_dir = sys._MEIPASS
    else:
        bundle_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    ffmpeg_dir = os.path.join(bundle_dir, 'ffmpeg')
    ffmpeg_name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    ffmpeg_path = os.path.join(ffmpeg_dir, ffmpeg_name)

    if os.path.isfile(ffmpeg_path):
        return ffmpeg_path
    return 'ffmpeg'  # Fall back to PATH


def _stitch_videos_sync(video_path, clips, output_path, progress_state=None):
    """
    Extract clips from a single video and concatenate them using FFmpeg.

    Args:
        video_path: Path to source video file
        clips: List of dicts with 'start' and 'end' in seconds
        output_path: Full path for output video file
        progress_state: Optional dict for progress reporting
    """
    _cancel_event.clear()

    if progress_state is not None:
        progress_state["stage"] = "preparing"
        progress_state["percent"] = 0

    try:
        if not os.path.isfile(video_path):
            return {"success": False, "error": f"Source video not found: {video_path}"}

        if not clips or len(clips) == 0:
            return {"success": False, "error": "No clips provided"}

        ffmpeg = _get_ffmpeg_path()
        temp_dir = tempfile.mkdtemp(prefix="preview_stitch_")
        segment_files = []

        try:
            total_clips = len(clips)

            # Step 1: Extract each clip segment
            for i, clip in enumerate(clips):
                if _cancel_event.is_set():
                    return {"success": False, "cancelled": True}

                start = clip["start"]
                end = clip["end"]
                duration = end - start

                segment_path = os.path.join(temp_dir, f"segment_{i:03d}.mp4")
                segment_files.append(segment_path)

                cmd = [
                    ffmpeg,
                    '-y',
                    '-ss', str(start),
                    '-i', video_path,
                    '-t', str(duration),
                    '-c', 'copy',
                    '-avoid_negative_ts', 'make_zero',
                    segment_path
                ]

                kwargs = {}
                if sys.platform == "win32":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    **kwargs
                )
                _, stderr = proc.communicate()

                if proc.returncode != 0:
                    error_msg = stderr.decode('utf-8', errors='replace').strip()
                    # If stream copy fails, retry with re-encoding
                    cmd_reencode = [
                        ffmpeg,
                        '-y',
                        '-ss', str(start),
                        '-i', video_path,
                        '-t', str(duration),
                        '-c:v', 'libx264',
                        '-preset', 'fast',
                        '-crf', '23',
                        '-c:a', 'aac',
                        '-b:a', '128k',
                        '-avoid_negative_ts', 'make_zero',
                        segment_path
                    ]
                    proc2 = subprocess.Popen(
                        cmd_reencode,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        **kwargs
                    )
                    _, stderr2 = proc2.communicate()
                    if proc2.returncode != 0:
                        error_msg2 = stderr2.decode('utf-8', errors='replace').strip()
                        return {"success": False, "error": f"FFmpeg segment {i} failed: {error_msg2}"}

                if progress_state is not None:
                    progress_state["stage"] = "extracting"
                    progress_state["percent"] = int(((i + 1) / total_clips) * 70)

            if _cancel_event.is_set():
                return {"success": False, "cancelled": True}

            # Step 2: Create concat file
            concat_file = os.path.join(temp_dir, "concat.txt")
            with open(concat_file, 'w') as f:
                for seg in segment_files:
                    escaped = seg.replace('\\', '/').replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            if progress_state is not None:
                progress_state["stage"] = "concatenating"
                progress_state["percent"] = 75

            # Step 3: Concatenate
            cmd_concat = [
                ffmpeg,
                '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c', 'copy',
                output_path
            ]

            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            proc = subprocess.Popen(
                cmd_concat,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                **kwargs
            )
            _, stderr = proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='replace').strip()
                # Retry with re-encoding if concat fails
                cmd_concat_reencode = [
                    ffmpeg,
                    '-y',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', concat_file,
                    '-c:v', 'libx264',
                    '-preset', 'fast',
                    '-crf', '23',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    output_path
                ]
                proc2 = subprocess.Popen(
                    cmd_concat_reencode,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    **kwargs
                )
                _, stderr2 = proc2.communicate()
                if proc2.returncode != 0:
                    error_msg2 = stderr2.decode('utf-8', errors='replace').strip()
                    return {"success": False, "error": f"FFmpeg concat failed: {error_msg2}"}

            if progress_state is not None:
                progress_state["stage"] = "done"
                progress_state["percent"] = 100
                progress_state["done"] = True

            # Calculate total duration
            total_duration = sum(c["end"] - c["start"] for c in clips)

            return {
                "success": True,
                "output_path": output_path,
                "duration": round(total_duration, 2),
                "clip_count": len(clips)
            }

        finally:
            # Clean up temp segments
            import shutil
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    except Exception as e:
        logger.exception("Video stitching failed")
        if progress_state is not None:
            progress_state["done"] = True
        return {"success": False, "error": f"Stitching failed: {str(e)}"}


_stitch_progress = {"stage": "idle", "percent": 0, "done": False, "active": False}


def get_stitch_progress():
    """Return a snapshot of current stitch progress."""
    return dict(_stitch_progress)


def start_stitch_background(video_path, clips, output_path):
    """Start stitching in background with progress tracking. Returns the future.
    Must be called from within a running event loop."""
    from .config import EXECUTOR

    _stitch_progress["stage"] = "preparing"
    _stitch_progress["percent"] = 0
    _stitch_progress["done"] = False
    _stitch_progress["active"] = True
    _stitch_progress["result"] = None

    loop = asyncio.get_running_loop()

    def _run():
        result = _stitch_videos_sync(video_path, clips, output_path, _stitch_progress)
        _stitch_progress["result"] = result
        _stitch_progress["done"] = True
        _stitch_progress["active"] = False
        return result

    return loop.run_in_executor(EXECUTOR, _run)
