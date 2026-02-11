import asyncio
import base64
import logging

from .scene_detector import detect_scenes_with_progress, cancel_detection

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


async def handle_detect_scenes(websocket, msg, command, request_id):
    """Start scene detection as a background task. Returns None to signal no immediate response."""
    logger.info("WS command: detect_scenes (with progress)")

    async def _run(ws, cmd, rid, vpath, thresh, ds, fskip):
        async for update in detect_scenes_with_progress(
            video_path=vpath,
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
            msg["videoPath"],
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
    "ping": handle_ping,
}
