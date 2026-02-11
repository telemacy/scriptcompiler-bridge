import asyncio
import logging
import threading

from .config import EXECUTOR

logger = logging.getLogger(__name__)

# Module-level cancel event - set from async side, checked from sync side
_cancel_event = threading.Event()


def _detect_scenes_sync(video_path, threshold, downscale=0, frame_skip=0, progress_state=None):
    """Run PySceneDetect synchronously (called in executor thread).

    Uses the official detect_scenes API in chunked calls (5 seconds each)
    so we can check for cancellation between chunks while keeping full accuracy.
    """
    try:
        from scenedetect import open_video, SceneManager, ContentDetector, FrameTimecode
    except ImportError as e:
        return {
            "success": False,
            "error": f"Missing dependency: {e}",
        }

    _cancel_event.clear()

    try:
        video = open_video(video_path, backend="opencv")
        total_frames = video.duration.get_frames()
        fps = video.frame_rate
        width = video.frame_size[0] if hasattr(video, 'frame_size') else 0
        height = video.frame_size[1] if hasattr(video, 'frame_size') else 0

        # Auto-downscale based on resolution if not specified
        if downscale <= 0:
            if height >= 2160:
                downscale = 4
            elif height >= 1080:
                downscale = 3
            elif height >= 720:
                downscale = 2
            else:
                downscale = 1

        logger.info(
            "Scene detection: %s (%dx%d, %d frames, %.1f fps, threshold=%.1f, downscale=%d, frame_skip=%d)",
            video_path, width, height, total_frames, fps, threshold, downscale, frame_skip
        )

        if total_frames <= 0:
            return {"success": False, "error": "Could not determine video duration"}

        # Apply downscale via PySceneDetect's built-in support
        if downscale > 1:
            video.downscale = downscale

        # Share video ref and total frames for progress polling
        if progress_state is not None:
            progress_state["video"] = video
            progress_state["total_frames"] = total_frames

        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=threshold))

        # Process in 5-second chunks so we can check cancel flag between them.
        # detect_scenes accumulates results across multiple calls on the same SceneManager.
        chunk_frames = int(5.0 * fps)
        last_scene_count = 0

        while True:
            if _cancel_event.is_set():
                logger.info("Scene detection cancelled at frame %d/%d",
                            video.frame_number, total_frames)
                if progress_state is not None:
                    progress_state["done"] = True
                return {"success": False, "cancelled": True, "error": "Cancelled by user"}

            # Calculate end_time for this chunk based on current position + chunk size
            current_frame = video.frame_number
            end_frame = min(current_frame + chunk_frames, total_frames)
            end_time = FrameTimecode(end_frame, fps)

            n = scene_manager.detect_scenes(
                video=video,
                end_time=end_time,
                frame_skip=frame_skip,
            )

            # Share partial scene results for progressive display
            if progress_state is not None:
                current_scenes = scene_manager.get_scene_list()
                if len(current_scenes) > last_scene_count:
                    last_scene_count = len(current_scenes)
                    progress_state["partial_scenes"] = [
                        {
                            "start": s.get_seconds(),
                            "end": e.get_seconds(),
                            "startFrame": s.get_frames(),
                            "endFrame": e.get_frames(),
                        }
                        for s, e in current_scenes
                    ]

            # If no frames were processed, we've reached the end
            if n == 0:
                break

        if progress_state is not None:
            progress_state["done"] = True

        scene_list = scene_manager.get_scene_list()
        logger.info("Scene detection complete: found %d scenes in %d frames",
                     len(scene_list), total_frames)

        scenes = []
        for start, end in scene_list:
            scenes.append({
                "start": start.get_seconds(),
                "end": end.get_seconds(),
                "startFrame": start.get_frames(),
                "endFrame": end.get_frames(),
            })

        return {"success": True, "scenes": scenes, "sceneCount": len(scenes)}

    except Exception as e:
        if progress_state is not None:
            progress_state["done"] = True
        return {"success": False, "error": f"Scene detection failed: {str(e)}"}


def cancel_detection():
    """Signal the running detection to stop."""
    _cancel_event.set()


async def detect_scenes(video_path, threshold=30.0):
    """Detect scene boundaries in a video file (no progress)."""
    if not video_path:
        return {"success": False, "error": "No video path provided"}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(EXECUTOR, _detect_scenes_sync, video_path, threshold, 0, 0, None)


async def detect_scenes_with_progress(video_path, threshold=30.0, downscale=0, frame_skip=0):
    """Detect scenes with async progress generator.

    Yields progress dicts: { "type": "progress", "framesProcessed": N, "totalFrames": N, "percent": N }
    Final yield: { "type": "result", "success": ..., "scenes": ..., "sceneCount": ... }
    """
    if not video_path:
        yield {"type": "result", "success": False, "error": "No video path provided"}
        return

    loop = asyncio.get_event_loop()

    # Shared state for progress polling
    progress_state = {"video": None, "total_frames": 0, "done": False}

    # Start detection in background thread
    future = loop.run_in_executor(
        EXECUTOR, _detect_scenes_sync, video_path, threshold, downscale, frame_skip, progress_state
    )

    last_percent = -1

    # Poll progress while detection runs
    while not future.done():
        await asyncio.sleep(0.5)

        video = progress_state.get("video")
        total = progress_state.get("total_frames", 0)

        if video and total > 0 and not progress_state.get("done"):
            try:
                current = video.frame_number
                percent = min(round((current / total) * 100), 99)
            except Exception:
                continue

            if percent != last_percent:
                last_percent = percent
                update = {
                    "type": "progress",
                    "framesProcessed": current,
                    "totalFrames": total,
                    "percent": percent,
                }
                partial = progress_state.get("partial_scenes")
                if partial:
                    update["scenes"] = partial
                    update["sceneCount"] = len(partial)
                yield update

    result = await future
    result["type"] = "result"
    yield result
