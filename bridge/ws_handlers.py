import asyncio
import base64
import logging

from .scene_detector import detect_scenes_with_progress, cancel_detection
from .audio_analyzer import analyze_audio_with_progress, cancel_audio_analysis
from .thumbnail_cache import pregenerate_with_progress, cancel_pregeneration
from .settings import get_video_folders
from .video_library import is_path_in_allowed_folders
from .file_handler import is_dialog_allowed_path

logger = logging.getLogger(__name__)


async def handle_initialize(tracker, msg):
    logger.info("WS command: initialize")
    result = await tracker.initialize()
    logger.info("Initialize result: success=%s", result.get("success"))
    return result


async def handle_start_tracking(tracker, msg, frame_bytes):
    if frame_bytes is not None:
        frame_data = base64.b64encode(frame_bytes).decode("ascii")
    else:
        frame_data = msg["frameData"]

    logger.info(
        "WS command: start_tracking (width=%s, height=%s, point=%s, binary=%s)",
        msg.get("width"), msg.get("height"), msg.get("trackingPoint"), frame_bytes is not None,
    )
    result = await tracker.start_tracking(
        frame_data=frame_data,
        width=msg["width"],
        height=msg["height"],
        tracking_point=msg["trackingPoint"],
        bounding_box=msg.get("boundingBox"),
        video_size=msg.get("videoSize"),
        settings=msg.get("settings"),
    )
    logger.info("start_tracking result: success=%s method=%s", result.get("success"), result.get("method"))
    return result


async def handle_process_frame(tracker, msg, frame_bytes):
    if frame_bytes is not None:
        frame_data = base64.b64encode(frame_bytes).decode("ascii")
    else:
        frame_data = msg["frameData"]

    return await tracker.process_frame(
        frame_data=frame_data,
        width=msg["width"],
        height=msg["height"],
        bounding_box=msg.get("boundingBox"),
        video_size=msg.get("videoSize"),
    )


async def handle_stop_tracking(tracker, msg):
    logger.info("WS command: stop_tracking")
    return await tracker.stop_tracking()


async def handle_cleanup(tracker, msg):
    logger.info("WS command: cleanup")
    return await tracker.cleanup()


def _is_allowed_path(path):
    """Check if a path is in video folders or was returned by a file dialog."""
    folders = get_video_folders()
    return is_path_in_allowed_folders(path, folders) or is_dialog_allowed_path(path)


async def handle_detect_scenes(websocket, msg, command, request_id):
    """Start scene detection as a background task. Returns None to signal no immediate response."""
    logger.info("WS command: detect_scenes (with progress)")

    vpath = msg.get("videoPath")
    if not vpath or not _is_allowed_path(vpath):
        resp = {"type": "result", "command": command, "success": False, "error": "Access denied"}
        if request_id is not None:
            resp["_requestId"] = request_id
        await websocket.send_json(resp)
        return None

    async def _run(ws, cmd, rid, vp, thresh, ds, fskip):
        async for update in detect_scenes_with_progress(
            video_path=vp,
            threshold=thresh,
            downscale=ds,
            frame_skip=fskip,
        ):
            update["command"] = cmd
            if rid is not None:
                update["_requestId"] = rid
            await ws.send_json(update)

    task = asyncio.create_task(
        _run(
            websocket, command, request_id,
            vpath,
            msg.get("threshold", 30.0),
            msg.get("downscale", 0),
            msg.get("frameSkip", 0),
        )
    )
    return task  # caller stores ref for cleanup on disconnect


async def handle_cancel_scene_detection(msg):
    logger.info("WS command: cancel_scene_detection")
    cancel_detection()
    return {"success": True}


async def handle_analyze_audio(websocket, msg, command, request_id):
    """Start audio analysis as a background task. Returns task ref for cleanup."""
    logger.info("WS command: analyze_audio (with progress)")

    vpath = msg.get("videoPath") or msg.get("audioPath")
    if not vpath or not _is_allowed_path(vpath):
        resp = {"type": "result", "command": command, "success": False, "error": "Access denied"}
        if request_id is not None:
            resp["_requestId"] = request_id
        await websocket.send_json(resp)
        return None

    async def _run(ws, cmd, rid, vp, opts):
        async for update in analyze_audio_with_progress(
            video_path=vp,
            options=opts,
        ):
            update["command"] = cmd
            if rid is not None:
                update["_requestId"] = rid
            await ws.send_json(update)

    task = asyncio.create_task(
        _run(
            websocket, command, request_id,
            vpath,
            msg.get("options", {}),
        )
    )
    return task


async def handle_cancel_audio_analysis(msg):
    logger.info("WS command: cancel_audio_analysis")
    cancel_audio_analysis()
    return {"success": True}


async def handle_pregenerate_thumbnails(websocket, msg, command, request_id):
    """Start thumbnail pregeneration as a background task. Returns task ref for cleanup."""
    logger.info("WS command: pregenerate_thumbnails")

    vpath = msg.get("videoPath")
    if not vpath or not _is_allowed_path(vpath):
        resp = {"type": "result", "command": command, "success": False, "error": "Access denied"}
        if request_id is not None:
            resp["_requestId"] = request_id
        await websocket.send_json(resp)
        return None

    async def _run(ws, cmd, rid, vp, times):
        async for update in pregenerate_with_progress(vp, times):
            update["command"] = cmd
            if rid is not None:
                update["_requestId"] = rid
            await ws.send_json(update)

    task = asyncio.create_task(
        _run(
            websocket, command, request_id,
            vpath,
            msg.get("times", []),
        )
    )
    return task


async def handle_cancel_thumbnail_pregeneration(msg):
    logger.info("WS command: cancel_thumbnail_pregeneration")
    cancel_pregeneration()
    return {"success": True}


async def handle_ping(msg):
    return {"success": True, "pong": True}


# Command -> handler mapping
# Handlers that need (tracker, msg, frame_bytes) get those args from the dispatch loop.
HANDLERS = {
    "initialize": handle_initialize,
    "start_tracking": handle_start_tracking,
    "process_frame": handle_process_frame,
    "stop_tracking": handle_stop_tracking,
    "cleanup": handle_cleanup,
    "detect_scenes": handle_detect_scenes,
    "cancel_scene_detection": handle_cancel_scene_detection,
    "analyze_audio": handle_analyze_audio,
    "cancel_audio_analysis": handle_cancel_audio_analysis,
    "pregenerate_thumbnails": handle_pregenerate_thumbnails,
    "cancel_thumbnail_pregeneration": handle_cancel_thumbnail_pregeneration,
    "ping": handle_ping,
}
