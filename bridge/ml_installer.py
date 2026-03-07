import asyncio
import logging
import os
import subprocess
import sys
import threading

from .config import SETTINGS_DIR_NAME

logger = logging.getLogger(__name__)

# Installation state
_install_lock = threading.Lock()
_install_progress = {"active": False, "stage": "", "percent": 0, "done": False, "error": None}

# Where ML packages get installed
ML_PACKAGES_DIR_NAME = "ml_packages"


def get_ml_packages_dir():
    return os.path.join(str(os.path.expanduser("~")), SETTINGS_DIR_NAME, ML_PACKAGES_DIR_NAME)


def setup_ml_path():
    """Add ML packages directory to sys.path so imports work.
    Call this early at startup."""
    ml_dir = get_ml_packages_dir()
    if os.path.isdir(ml_dir) and ml_dir not in sys.path:
        sys.path.insert(0, ml_dir)
        logger.info("Added ML packages path: %s", ml_dir)


def _get_python_executable():
    """Get the Python executable path, works in both dev and frozen (PyInstaller) modes."""
    if getattr(sys, 'frozen', False):
        # PyInstaller bundle: python is in _internal
        bundle_dir = sys._MEIPASS
        if sys.platform == 'win32':
            python = os.path.join(bundle_dir, 'python.exe')
        else:
            python = os.path.join(bundle_dir, 'python')
        if os.path.isfile(python):
            return python
    return sys.executable


def get_ml_status():
    """Check which ML packages are installed and available."""
    status = {
        "torch": False,
        "demucs": False,
        "beat_this": False,
        "allin1": False,
        "gpu_available": False,
        "install_active": _install_progress.get("active", False),
        "install_dir": get_ml_packages_dir(),
    }

    try:
        import torch
        status["torch"] = True
        status["gpu_available"] = torch.cuda.is_available()
    except ImportError:
        pass

    try:
        import demucs  # noqa: F401
        status["demucs"] = True
    except ImportError:
        pass

    try:
        import beat_this  # noqa: F401
        status["beat_this"] = True
    except ImportError:
        pass

    try:
        import allin1  # noqa: F401
        status["allin1"] = True
    except ImportError:
        pass

    status["all_installed"] = all([
        status["torch"], status["demucs"],
        status["beat_this"], status["allin1"]
    ])

    return status


def get_install_progress():
    return dict(_install_progress)


def cancel_ml_install():
    _install_progress["cancelled"] = True


def _get_pip_executable(target_dir):
    """Find pip in the target directory or as a module."""
    # Check for pip installed in target dir (Scripts/pip or bin/pip)
    if sys.platform == 'win32':
        pip_script = os.path.join(target_dir, 'Scripts', 'pip.exe')
    else:
        pip_script = os.path.join(target_dir, 'bin', 'pip')
    if os.path.isfile(pip_script):
        return [pip_script]

    # Check if pip module is available via the bundled python
    python = _get_python_executable()
    try:
        result = subprocess.run(
            [python, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": target_dir},
        )
        if result.returncode == 0:
            return [python, "-m", "pip"]
    except Exception:
        pass

    return None


def _run_pip(args, target_dir, progress_state, stage_name, percent_start, percent_end):
    """Run a pip install command and track progress."""
    pip_cmd = _get_pip_executable(target_dir)
    if not pip_cmd:
        return False, "pip not found"

    cmd = pip_cmd + [
        "install",
        "--target", target_dir,
        "--no-warn-script-location",
        "--disable-pip-version-check",
    ] + args

    # Set PYTHONPATH so pip can find itself in target_dir
    env = {**os.environ, "PYTHONPATH": target_dir}

    progress_state["stage"] = stage_name
    progress_state["percent"] = percent_start

    logger.info("ML install: running %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

        output_lines = []
        for line in proc.stdout:
            line = line.strip()
            if line:
                output_lines.append(line)
                logger.info("pip: %s", line)

                # Estimate progress from pip output
                if "Downloading" in line:
                    progress_state["percent"] = min(
                        percent_start + (percent_end - percent_start) // 2,
                        percent_end - 1
                    )
                elif "Installing" in line:
                    progress_state["percent"] = percent_end - 5

            if progress_state.get("cancelled"):
                proc.kill()
                return False, "Cancelled by user"

        proc.wait()

        if proc.returncode != 0:
            error_msg = "\n".join(output_lines[-5:])
            return False, f"pip failed (exit {proc.returncode}): {error_msg}"

        progress_state["percent"] = percent_end
        return True, None

    except Exception as e:
        return False, str(e)


def _ensure_pip(target_dir):
    """Make sure pip is available, downloading get-pip.py if needed."""
    # Check if pip is already usable
    if _get_pip_executable(target_dir):
        logger.info("pip already available")
        return True

    python = _get_python_executable()

    # Try ensurepip first (works in non-frozen environments)
    if not getattr(sys, 'frozen', False):
        try:
            result = subprocess.run(
                [python, "-m", "ensurepip", "--default-pip"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    # Download get-pip.py and install pip into target directory
    logger.info("Downloading get-pip.py to bootstrap pip...")
    get_pip_path = os.path.join(target_dir, "get-pip.py")

    try:
        import urllib.request
        urllib.request.urlretrieve(
            "https://bootstrap.pypa.io/get-pip.py",
            get_pip_path
        )
    except Exception as e:
        logger.error("Failed to download get-pip.py: %s", e)
        return False

    # Run get-pip.py to install pip into the target directory
    try:
        result = subprocess.run(
            [python, get_pip_path, "--target", target_dir, "--no-warn-script-location"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": target_dir},
        )
        logger.info("get-pip.py output: %s", result.stdout[-500:] if result.stdout else "")
        if result.returncode != 0:
            logger.error("get-pip.py failed: %s", result.stderr[-500:] if result.stderr else "")
            return False
    except Exception as e:
        logger.error("Failed to run get-pip.py: %s", e)
        return False
    finally:
        # Clean up get-pip.py
        try:
            os.remove(get_pip_path)
        except OSError:
            pass

    # Verify pip is now available
    return _get_pip_executable(target_dir) is not None


def _install_ml_packages_sync():
    """Install all ML packages synchronously. Called from executor."""
    global _install_progress

    _install_progress = {
        "active": True, "stage": "preparing", "percent": 0,
        "done": False, "error": None, "cancelled": False
    }

    target_dir = get_ml_packages_dir()
    os.makedirs(target_dir, exist_ok=True)

    try:
        # Step 1: Ensure pip is available
        _install_progress["stage"] = "checking_pip"
        _install_progress["percent"] = 2

        if not _ensure_pip(target_dir):
            _install_progress["error"] = "pip is not available and could not be bootstrapped"
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "error": _install_progress["error"]}

        if _install_progress.get("cancelled"):
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "cancelled": True}

        # Step 2: Install torch + torchaudio (largest, ~2GB)
        ok, err = _run_pip(
            ["torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cu121"],
            target_dir, _install_progress,
            "installing_torch", 5, 50
        )
        if not ok:
            # Fallback to CPU-only torch if CUDA install fails
            logger.warning("CUDA torch install failed, trying CPU-only: %s", err)
            _install_progress["stage"] = "installing_torch_cpu"
            ok, err = _run_pip(
                ["torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cpu"],
                target_dir, _install_progress,
                "installing_torch_cpu", 10, 50
            )
            if not ok:
                _install_progress["error"] = f"Failed to install torch: {err}"
                _install_progress["done"] = True
                _install_progress["active"] = False
                return {"success": False, "error": _install_progress["error"]}

        if _install_progress.get("cancelled"):
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "cancelled": True}

        # Step 3: Install demucs
        ok, err = _run_pip(
            ["demucs>=4.0.0"],
            target_dir, _install_progress,
            "installing_demucs", 50, 70
        )
        if not ok:
            _install_progress["error"] = f"Failed to install demucs: {err}"
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "error": _install_progress["error"]}

        if _install_progress.get("cancelled"):
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "cancelled": True}

        # Step 4: Install allin1
        ok, err = _run_pip(
            ["allin1>=1.1.0"],
            target_dir, _install_progress,
            "installing_allin1", 70, 85
        )
        if not ok:
            _install_progress["error"] = f"Failed to install allin1: {err}"
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "error": _install_progress["error"]}

        if _install_progress.get("cancelled"):
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "cancelled": True}

        # Step 5: Install beat-this from GitHub
        ok, err = _run_pip(
            ["beat-this @ git+https://github.com/CPJKU/beat_this.git"],
            target_dir, _install_progress,
            "installing_beat_this", 85, 95
        )
        if not ok:
            _install_progress["error"] = f"Failed to install beat-this: {err}"
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "error": _install_progress["error"]}

        # Step 6: Add to sys.path and verify
        _install_progress["stage"] = "verifying"
        _install_progress["percent"] = 95

        setup_ml_path()

        status = get_ml_status()
        if not status["all_installed"]:
            missing = [k for k in ["torch", "demucs", "beat_this", "allin1"] if not status[k]]
            _install_progress["error"] = f"Verification failed, missing: {', '.join(missing)}"
            _install_progress["done"] = True
            _install_progress["active"] = False
            return {"success": False, "error": _install_progress["error"]}

        _install_progress["stage"] = "complete"
        _install_progress["percent"] = 100
        _install_progress["done"] = True
        _install_progress["active"] = False

        logger.info("ML packages installed successfully to %s", target_dir)
        return {"success": True, "gpu_available": status["gpu_available"]}

    except Exception as e:
        logger.error("ML package installation failed: %s", e, exc_info=True)
        _install_progress["error"] = str(e)
        _install_progress["done"] = True
        _install_progress["active"] = False
        return {"success": False, "error": str(e)}


async def install_ml_packages_with_progress():
    """Async generator that yields progress updates then final result."""
    if _install_progress.get("active"):
        yield {"type": "result", "success": False, "error": "Installation already in progress"}
        return

    loop = asyncio.get_running_loop()

    from .config import EXECUTOR
    future = loop.run_in_executor(EXECUTOR, _install_ml_packages_sync)

    last_percent = -1
    last_stage = ""

    while not future.done():
        await asyncio.sleep(1.0)

        if not _install_progress.get("done"):
            percent = _install_progress.get("percent", 0)
            stage = _install_progress.get("stage", "unknown")

            if percent != last_percent or stage != last_stage:
                last_percent = percent
                last_stage = stage
                yield {
                    "type": "progress",
                    "percent": percent,
                    "stage": stage,
                }

    result = await future
    result["type"] = "result"
    yield result
