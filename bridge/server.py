import asyncio
import json
import os
import struct
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from typing import Optional

from urllib.parse import quote as url_encode

from .config import BRIDGE_VERSION, BRIDGE_NAME, CORS_ALLOW_ORIGIN_REGEX, DEFAULT_PORT, EXECUTOR
from .tracker_bridge import TrackerBridge
from .file_handler import open_video_dialog, open_audio_dialog, open_funscript_dialog, save_funscript_dialog, write_funscript, is_dialog_allowed_path
from .scene_detector import detect_scenes, cancel_detection
from .video_stitcher import start_stitch_background, get_stitch_progress, cancel_stitching
from .audio_analyzer import cancel_audio_analysis
from .thumbnail_cache import cancel_pregeneration
from .stem_separator import cancel_stem_separation
from .music_analyzer import cancel_music_analysis
from .settings import get_video_folders, get_settings, update_settings
from .url_loader import start_download as ytdlp_start_download, fetch_video_info as ytdlp_fetch_video_info, get_active_downloads as ytdlp_get_active_downloads
from .updater import check_for_update, get_cached_update, download_and_run_update
from .video_library import (
    get_cached_videos, scan_and_cache, stream_video,
    is_path_in_allowed_folders, generate_thumbnail,
    generate_frame_at_time, generate_frames_batch,
)
from . import thumbnail_cache
from .ws_handlers import HANDLERS as WS_HANDLERS

logger = logging.getLogger(__name__)

app = FastAPI(title=BRIDGE_NAME, version=BRIDGE_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

tracker = TrackerBridge()
_shutdown_server = None


def set_shutdown_callback(cb):
    global _shutdown_server
    _shutdown_server = cb


# Active WebSocket connections for broadcasting download progress
_ws_connections: list = []


async def _broadcast_to_ws(message: dict):
    """Send a message to all connected WebSocket clients."""
    for ws in list(_ws_connections):
        try:
            await ws.send_json(message)
        except Exception:
            pass


# --- HTTP Endpoints ---

@app.get("/health")
async def health():
    resp = {
        "status": "ok",
        "name": BRIDGE_NAME,
        "version": BRIDGE_VERSION,
        "tracking": tracker.is_ready,
    }
    update = get_cached_update()
    if update and update.get("update_available"):
        resp["update"] = update
    return resp


@app.get("/capabilities")
async def capabilities():
    caps = ["files", "scenes", "tracking"]
    if get_video_folders():
        caps.append("local_videos")
    try:
        import librosa  # noqa: F401
        caps.append("audio_analysis")
    except ImportError:
        pass
    try:
        import demucs  # noqa: F401
        caps.append("stem_separation")
    except ImportError:
        pass
    try:
        import beat_this  # noqa: F401
        import allin1  # noqa: F401
        caps.append("music_analysis")
    except ImportError:
        pass
    return {
        "capabilities": caps,
        "version": BRIDGE_VERSION,
        "tracking_ready": tracker.is_ready,
    }


class SceneDetectRequest(BaseModel):
    videoPath: str
    threshold: Optional[float] = 30.0


@app.post("/scenes/detect")
async def detect_scenes_endpoint(req: SceneDetectRequest):
    folders = get_video_folders()
    if not is_path_in_allowed_folders(req.videoPath, folders) and not is_dialog_allowed_path(req.videoPath):
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    result = await detect_scenes(req.videoPath, req.threshold)
    return JSONResponse(content=result)


@app.post("/files/open-video")
async def open_video():
    result = await open_video_dialog()
    return JSONResponse(content=result or {"cancelled": True})


@app.post("/files/open-audio")
async def open_audio():
    result = await open_audio_dialog()
    return JSONResponse(content=result or {"cancelled": True})


@app.get("/files/stream")
async def stream_file(path: str):
    """Stream a file from disk. Used to load audio into the browser for playback."""
    folders = get_video_folders()
    if not is_path_in_allowed_folders(path, folders) and not is_dialog_allowed_path(path):
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(path)


@app.post("/files/open-funscript")
async def open_funscript():
    result = await open_funscript_dialog()
    return JSONResponse(content=result or {"cancelled": True})


class SaveFunscriptRequest(BaseModel):
    data: str
    defaultName: Optional[str] = "script.funscript"


@app.post("/files/save-funscript")
async def save_funscript(req: SaveFunscriptRequest):
    result = await save_funscript_dialog(req.data, req.defaultName)
    return JSONResponse(content=result or {"cancelled": True})


class WriteFunscriptRequest(BaseModel):
    data: str
    path: str


@app.post("/files/write-funscript")
async def write_funscript_endpoint(req: WriteFunscriptRequest):
    folders = get_video_folders()
    if not folders or not is_path_in_allowed_folders(req.path, folders):
        return JSONResponse(status_code=403, content={"success": False, "error": "Access denied"})
    result = await write_funscript(req.data, req.path)
    return JSONResponse(content=result)


# --- Updates ---

@app.get("/update/check")
async def check_update():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(EXECUTOR, check_for_update)
    return JSONResponse(content=result)


@app.post("/update/apply")
async def apply_update():
    result = download_and_run_update(shutdown_callback=_shutdown_server)
    return JSONResponse(content=result)


# --- Video Library ---

@app.get("/videos/list")
async def list_videos():
    folders = get_video_folders()
    videos = get_cached_videos()
    return JSONResponse(content={
        "videos": videos,
        "folders": folders,
        "count": len(videos),
    })


@app.get("/settings")
async def get_settings_endpoint():
    return JSONResponse(content=get_settings())


class UpdateSettingsRequest(BaseModel):
    yt_dlp_quality: Optional[str] = None


@app.post("/settings")
async def update_settings_endpoint(req: UpdateSettingsRequest):
    updates = req.model_dump(exclude_unset=True)
    return JSONResponse(content=update_settings(updates))


class LoadUrlRequest(BaseModel):
    url: str
    video_info: Optional[dict] = None


@app.post("/videos/load-url")
async def load_url_endpoint(req: LoadUrlRequest, request: Request):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "Invalid URL", "code": "INVALID_URL"})

    try:
        download_id, file_path = await ytdlp_start_download(url, _broadcast_to_ws, video_info=req.video_info)
    except ValueError as e:
        code = str(e)
        if code == "NO_VIDEO_FOLDER":
            return JSONResponse(status_code=400, content={
                "error": "No video folder configured. Add one in bridge settings.",
                "code": "NO_VIDEO_FOLDER"
            })
        return JSONResponse(status_code=400, content={"error": code, "code": "YTDLP_ERROR"})

    port = request.url.port or DEFAULT_PORT
    stream_url = f"http://127.0.0.1:{port}/videos/stream?path={url_encode(file_path)}"

    return JSONResponse(content={
        "stream_url": stream_url,
        "file_path": file_path,
        "download_id": download_id,
    })


class FetchInfoRequest(BaseModel):
    url: str


@app.post("/videos/fetch-info")
async def fetch_info_endpoint(req: FetchInfoRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "Invalid URL", "code": "INVALID_URL"})
    try:
        info = await ytdlp_fetch_video_info(url)
        return JSONResponse(content=info)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e), "code": "FETCH_INFO_ERROR"})


@app.get("/videos/stream")
async def stream_video_endpoint(path: str, request: Request):
    return await stream_video(path, request)


@app.get("/videos/active-downloads")
async def active_downloads_endpoint(request: Request):
    port = request.url.port or DEFAULT_PORT
    downloads = ytdlp_get_active_downloads()
    for dl in downloads:
        dl["stream_url"] = f"http://127.0.0.1:{port}/videos/stream?path={url_encode(dl['file_path'])}"
    return JSONResponse(content={"downloads": downloads})


@app.post("/videos/refresh")
async def refresh_videos():
    videos = scan_and_cache()
    folders = get_video_folders()
    return JSONResponse(content={
        "videos": videos,
        "folders": folders,
        "count": len(videos),
    })


class ClipRange(BaseModel):
    start: float
    end: float

class StitchRequest(BaseModel):
    video_path: str
    clips: list[ClipRange]
    output_name: str


_stitch_future = None


@app.post("/videos/stitch")
async def stitch_videos_endpoint(req: StitchRequest):
    global _stitch_future
    folders = get_video_folders()
    if not is_path_in_allowed_folders(req.video_path, folders) and not is_dialog_allowed_path(req.video_path):
        return JSONResponse(status_code=403, content={"error": "Access denied"})

    if not os.path.isfile(req.video_path):
        return JSONResponse(status_code=404, content={"error": "Video file not found"})

    if not req.clips or len(req.clips) == 0:
        return JSONResponse(status_code=400, content={"error": "No clips provided"})

    if len(req.clips) > 15:
        return JSONResponse(status_code=400, content={"error": "Maximum 15 clips allowed"})

    progress = get_stitch_progress()
    if progress.get("active"):
        return JSONResponse(status_code=409, content={"error": "Stitching already in progress"})

    # Determine output directory: first video folder or temp
    if folders:
        output_dir = folders[0]
    else:
        import tempfile
        output_dir = tempfile.gettempdir()

    output_path = os.path.join(output_dir, f"{req.output_name}.mp4")

    clips_data = [{"start": c.start, "end": c.end} for c in req.clips]
    _stitch_future = start_stitch_background(req.video_path, clips_data, output_path)

    return JSONResponse(content={"started": True})


@app.get("/videos/stitch/progress")
async def stitch_progress_endpoint():
    global _stitch_future
    progress = get_stitch_progress()

    # If done, include the final result from progress_state
    if progress.get("done"):
        result = progress.get("result")
        _stitch_future = None
        return JSONResponse(content={
            "done": True,
            "stage": progress.get("stage", "done"),
            "percent": progress.get("percent", 100),
            "result": result or {"success": False, "error": "No result available"}
        })

    return JSONResponse(content={
        "done": False,
        "stage": progress.get("stage", "idle"),
        "percent": progress.get("percent", 0),
        "active": progress.get("active", False)
    })


@app.post("/videos/stitch/cancel")
async def stitch_cancel_endpoint():
    cancel_stitching()
    return JSONResponse(content={"cancelled": True})


@app.get("/videos/thumbnail")
async def get_video_thumbnail(path: str):
    folders = get_video_folders()
    if not is_path_in_allowed_folders(path, folders):
        return JSONResponse(status_code=403, content={"error": "Access denied"})

    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "File not found"})

    loop = asyncio.get_event_loop()
    thumb_bytes = await loop.run_in_executor(EXECUTOR, generate_thumbnail, path)

    if thumb_bytes is None:
        return JSONResponse(status_code=500, content={"error": "Failed to generate thumbnail"})

    from fastapi.responses import Response
    return Response(content=thumb_bytes, media_type="image/jpeg", headers={
        "Cache-Control": "public, max-age=86400",
    })


@app.get("/videos/frame")
async def get_video_frame(path: str, time: float):
    folders = get_video_folders()
    if not is_path_in_allowed_folders(path, folders):
        return JSONResponse(status_code=403, content={"error": "Access denied"})

    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "File not found"})

    loop = asyncio.get_event_loop()
    frame_bytes = await loop.run_in_executor(EXECUTOR, generate_frame_at_time, path, time)

    if frame_bytes is None:
        return JSONResponse(status_code=500, content={"error": "Failed to extract frame"})

    from fastapi.responses import Response
    return Response(content=frame_bytes, media_type="image/jpeg", headers={
        "Cache-Control": "public, max-age=3600",
    })


class BatchFramesRequest(BaseModel):
    path: str
    times: list


@app.post("/videos/frames")
async def get_video_frames_batch(req: BatchFramesRequest):
    folders = get_video_folders()
    if not is_path_in_allowed_folders(req.path, folders):
        return JSONResponse(status_code=403, content={"error": "Access denied"})

    if not os.path.isfile(req.path):
        return JSONResponse(status_code=404, content={"error": "File not found"})

    times = req.times[:500]

    loop = asyncio.get_event_loop()

    # Check disk cache first
    cached = await loop.run_in_executor(
        EXECUTOR, thumbnail_cache.get_cached_frames_batch, req.path, times
    )

    # Extract only uncached frames via OpenCV
    uncached_times = [t for t in times if t not in cached]
    if uncached_times:
        extracted = await loop.run_in_executor(EXECUTOR, generate_frames_batch, req.path, uncached_times)
        if extracted:
            # Save newly extracted frames to disk cache in background
            loop.run_in_executor(EXECUTOR, thumbnail_cache.save_frames_batch, req.path, extracted)
            cached.update(extracted)

    import base64
    encoded = {}
    for t, frame_bytes in cached.items():
        encoded[str(t)] = base64.b64encode(frame_bytes).decode("ascii")

    return JSONResponse(content={"frames": encoded})


@app.get("/videos/funscript")
async def get_video_funscript(path: str):
    folders = get_video_folders()
    if not is_path_in_allowed_folders(path, folders):
        return JSONResponse(status_code=403, content={"found": False, "error": "Access denied"})

    base = os.path.splitext(path)[0]
    funscript_path = base + ".funscript"

    if not os.path.isfile(funscript_path):
        return JSONResponse(content={"found": False})

    try:
        with open(funscript_path, "r", encoding="utf-8") as f:
            content = f.read()
        return JSONResponse(content={
            "found": True,
            "path": funscript_path,
            "name": os.path.basename(funscript_path),
            "content": content,
        })
    except Exception as e:
        return JSONResponse(content={"found": False, "error": str(e)})


# --- WebSocket Tracking ---

def _parse_ws_message(ws_msg):
    """Parse a raw WebSocket message into (msg_dict, frame_bytes) or raise ValueError."""
    if "text" in ws_msg and ws_msg["text"]:
        return json.loads(ws_msg["text"]), None

    if "bytes" in ws_msg and ws_msg["bytes"]:
        data = ws_msg["bytes"]
        if len(data) < 2:
            raise ValueError("Binary message too short")
        header_len = struct.unpack(">H", data[:2])[0]
        if len(data) < 2 + header_len:
            raise ValueError("Incomplete binary header")
        msg = json.loads(data[2:2 + header_len])
        return msg, data[2 + header_len:]

    return None, None


@app.websocket("/ws/tracking")
async def tracking_ws(websocket: WebSocket):
    await websocket.accept()
    _ws_connections.append(websocket)
    logger.info("Tracking WebSocket connected")

    scene_detect_task = None
    audio_analyze_task = None
    thumbnail_pregen_task = None
    stem_separation_task = None
    music_analyze_task = None

    if not tracker.is_ready:
        init_result = await tracker.initialize()
        if not init_result.get("success"):
            logger.warning("Tracker initialization failed on WebSocket connect: %s", init_result.get("error"))

    try:
        while True:
            ws_msg = await websocket.receive()

            try:
                msg, frame_bytes = _parse_ws_message(ws_msg)
            except (json.JSONDecodeError, ValueError) as e:
                await websocket.send_json({"success": False, "error": str(e)})
                continue

            if msg is None:
                continue

            command = msg.get("command")
            request_id = msg.get("_requestId")

            try:
                handler = WS_HANDLERS.get(command)
                if handler is None:
                    logger.warning("Unknown WS command: %s", command)
                    result = {"success": False, "error": f"Unknown command: {command}"}
                elif command in ("start_tracking", "process_frame"):
                    result = await handler(tracker, msg, frame_bytes)
                elif command == "detect_scenes":
                    result = await handler(websocket, msg, command, request_id)
                    scene_detect_task = result  # store task ref for cleanup
                    result = None  # handler manages its own responses
                elif command == "analyze_audio":
                    result = await handler(websocket, msg, command, request_id)
                    audio_analyze_task = result
                    result = None
                elif command == "pregenerate_thumbnails":
                    result = await handler(websocket, msg, command, request_id)
                    thumbnail_pregen_task = result
                    result = None
                elif command == "separate_stems":
                    result = await handler(websocket, msg, command, request_id)
                    stem_separation_task = result
                    result = None
                elif command == "analyze_music":
                    result = await handler(websocket, msg, command, request_id)
                    music_analyze_task = result
                    result = None
                elif command in ("cancel_scene_detection", "cancel_audio_analysis", "cancel_thumbnail_pregeneration", "cancel_stem_separation", "cancel_music_analysis", "cancel_download", "ping"):
                    result = await handler(msg)
                    if command == "cancel_scene_detection":
                        scene_detect_task = None
                    elif command == "cancel_audio_analysis":
                        audio_analyze_task = None
                    elif command == "cancel_thumbnail_pregeneration":
                        thumbnail_pregen_task = None
                    elif command == "cancel_stem_separation":
                        stem_separation_task = None
                    elif command == "cancel_music_analysis":
                        music_analyze_task = None
                else:
                    result = await handler(tracker, msg)

                if result is None:
                    continue  # handler manages its own responses (e.g. detect_scenes)

                result["command"] = command
                if request_id is not None:
                    result["_requestId"] = request_id
                await websocket.send_json(result)

            except Exception as e:
                logger.error("Tracking command error (%s): %s", command, e)
                error_result = {
                    "command": command,
                    "success": False,
                    "error": str(e),
                }
                if request_id is not None:
                    error_result["_requestId"] = request_id
                await websocket.send_json(error_result)

    except WebSocketDisconnect:
        logger.info("Tracking WebSocket disconnected")
    except Exception as e:
        logger.error("Tracking WebSocket error: %s", e)
    finally:
        if websocket in _ws_connections:
            _ws_connections.remove(websocket)
        # Cancel any running scene detection
        if scene_detect_task is not None:
            cancel_detection()
            scene_detect_task.cancel()
            logger.info("Cancelled scene detection due to WebSocket disconnect")
        # Cancel any running audio analysis
        if audio_analyze_task is not None:
            cancel_audio_analysis()
            audio_analyze_task.cancel()
            logger.info("Cancelled audio analysis due to WebSocket disconnect")
        # Cancel any running thumbnail pregeneration
        if thumbnail_pregen_task is not None:
            cancel_pregeneration()
            thumbnail_pregen_task.cancel()
            logger.info("Cancelled thumbnail pregeneration due to WebSocket disconnect")
        if stem_separation_task is not None:
            cancel_stem_separation()
            stem_separation_task.cancel()
            logger.info("Cancelled stem separation due to WebSocket disconnect")
        if music_analyze_task is not None:
            cancel_music_analysis()
            music_analyze_task.cancel()
            logger.info("Cancelled music analysis due to WebSocket disconnect")
        if tracker.is_ready:
            try:
                await tracker.stop_tracking()
            except Exception:
                pass


# --- Lifecycle ---

@app.on_event("startup")
async def startup_event():
    logger.info("Starting %s v%s", BRIDGE_NAME, BRIDGE_VERSION)
    result = await tracker.initialize()
    if result.get("success"):
        logger.info("Tracker ready: OpenCV %s", result.get("opencvVersion"))
    else:
        logger.warning("Tracker not available: %s", result.get("error"))

    # Check for updates (await so it's ready before first /health request)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(EXECUTOR, check_for_update)

    # Clean up old thumbnail caches in background
    loop.run_in_executor(EXECUTOR, thumbnail_cache.cleanup_old_caches)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down bridge")
    await tracker.cleanup()
