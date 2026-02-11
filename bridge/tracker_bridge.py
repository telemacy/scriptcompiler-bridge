import asyncio
import json
import sys
import os
import logging

from .config import TRACKING_COMMAND_TIMEOUT, SCENE_DETECT_TIMEOUT

logger = logging.getLogger(__name__)


class TrackerBridge:
    """Manages the tracker.py subprocess. Mirrors Electron's tracking-manager.js."""

    def __init__(self):
        self._process = None
        self._is_ready = False
        self._startup_info = None
        self._request_id = 0
        self._pending = {}  # request_id -> asyncio.Future
        self._read_task = None
        self._buffer = ""

    @property
    def is_ready(self):
        return self._is_ready and self._process is not None

    async def initialize(self):
        if self.is_ready:
            return {"success": True, "alreadyRunning": True, **(self._startup_info or {})}

        await self.cleanup()

        if getattr(sys, 'frozen', False):
            # PyInstaller bundle: tracker binary is next to main executable
            exe_dir = os.path.dirname(sys.executable)
            if sys.platform == "win32":
                tracker_exe = os.path.join(exe_dir, "tracker.exe")
            else:
                tracker_exe = os.path.join(exe_dir, "tracker")
        else:
            tracker_exe = None

        tracker_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tracker.py")
        python_exe = sys.executable

        try:
            if tracker_exe and os.path.exists(tracker_exe):
                cmd = [tracker_exe]
            else:
                cmd = [python_exe, "-u", tracker_script]

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if sys.platform == "win32" else 0,  # CREATE_NO_WINDOW
            )
        except Exception as e:
            logger.error("Failed to spawn tracker subprocess: %s", e)
            return {"success": False, "error": f"Failed to spawn Python: {e}"}

        self._read_task = asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

        # Wait for startup message
        try:
            startup = await asyncio.wait_for(self._wait_for_startup(), timeout=10.0)
            if not startup.get("success"):
                return {"success": False, "error": startup.get("error", "Startup failed")}
            self._startup_info = startup
        except asyncio.TimeoutError:
            await self.cleanup()
            return {"success": False, "error": "Tracker startup timeout (10s)"}

        # Send ping to verify
        try:
            ping_result = await self.send_command({"command": "ping"}, timeout=5.0)
            if ping_result.get("pong"):
                self._is_ready = True
                return {
                    "success": True,
                    "opencvVersion": self._startup_info.get("opencv_version"),
                    "numpyVersion": self._startup_info.get("numpy_version"),
                }
            return {"success": False, "error": "Tracker did not respond to ping"}
        except Exception as e:
            await self.cleanup()
            return {"success": False, "error": f"Ping failed: {e}"}

    async def _wait_for_startup(self):
        """Wait for the startup message (no request ID)."""
        future = asyncio.get_event_loop().create_future()
        self._pending["__startup__"] = future
        return await future

    async def _read_stdout(self):
        """Continuously read JSON lines from the subprocess stdout."""
        try:
            while self._process and self._process.stdout:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    response = json.loads(line)
                    self._dispatch_response(response)
                except json.JSONDecodeError as e:
                    logger.error("Failed to parse tracker response: %s - %s", e, line)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Tracker stdout reader error: %s", e)
        finally:
            self._is_ready = False
            for rid, future in list(self._pending.items()):
                if not future.done():
                    future.set_exception(RuntimeError("Tracker process ended"))
            self._pending.clear()

    async def _read_stderr(self):
        """Log stderr from tracker subprocess."""
        try:
            while self._process and self._process.stderr:
                line_bytes = await self._process.stderr.readline()
                if not line_bytes:
                    break
                logger.warning("Tracker stderr: %s", line_bytes.decode("utf-8").strip())
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _dispatch_response(self, response):
        """Route a parsed JSON response to the right pending future."""
        # Startup message (no request ID)
        if response.get("command") == "startup" and "__startup__" in self._pending:
            future = self._pending.pop("__startup__")
            if not future.done():
                future.set_result(response)
            return

        if response.get("command") == "startup_error" and "__startup__" in self._pending:
            future = self._pending.pop("__startup__")
            if not future.done():
                future.set_result(response)
            return

        # Regular response with request ID
        rid = response.get("_requestId")
        if rid is not None and rid in self._pending:
            future = self._pending.pop(rid)
            response.pop("_requestId", None)
            if not future.done():
                future.set_result(response)

    async def send_command(self, command, timeout=None):
        """Send a JSON command to tracker.py and wait for the response."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("Tracker process not running")

        rid = self._request_id
        self._request_id += 1
        command["_requestId"] = rid

        future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future

        json_line = json.dumps(command) + "\n"
        self._process.stdin.write(json_line.encode("utf-8"))
        await self._process.stdin.drain()

        if timeout is None:
            timeout = SCENE_DETECT_TIMEOUT if command.get("command") == "detect_scenes" else TRACKING_COMMAND_TIMEOUT

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise RuntimeError(f"Tracker command timeout ({timeout}s): {command.get('command')}")

    async def start_tracking(self, frame_data, width, height, tracking_point, bounding_box, video_size, settings):
        return await self.send_command({
            "command": "start_tracking",
            "frameData": frame_data,
            "width": width,
            "height": height,
            "trackingPoint": tracking_point,
            "boundingBox": bounding_box,
            "videoSize": video_size,
            "trackingScale": (settings or {}).get("trackingScale", 1.0),
            "templateSize": (settings or {}).get("templateSize", 40),
        })

    async def process_frame(self, frame_data, width, height, bounding_box, video_size):
        return await self.send_command({
            "command": "process_frame",
            "frameData": frame_data,
            "width": width,
            "height": height,
            "boundingBox": bounding_box,
            "videoSize": video_size,
        })

    async def stop_tracking(self):
        if not self.is_ready:
            return {"success": True}
        return await self.send_command({"command": "stop_tracking"})

    async def cleanup(self):
        self._is_ready = False

        if self._process:
            try:
                if self._process.stdin and not self._process.stdin.is_closing():
                    cleanup_cmd = json.dumps({"command": "cleanup", "_requestId": -1}) + "\n"
                    self._process.stdin.write(cleanup_cmd.encode("utf-8"))
                    await self._process.stdin.drain()
            except Exception:
                pass

            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except Exception:
                    pass

        self._process = None
        self._startup_info = None

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        self._read_task = None

        for rid, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(RuntimeError("Tracker cleaned up"))
        self._pending.clear()

        return {"success": True}
