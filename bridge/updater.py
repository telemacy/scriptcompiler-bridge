import os
import sys
import tempfile
import subprocess
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError
import json

from .config import BRIDGE_VERSION, GITHUB_REPO

logger = logging.getLogger(__name__)

_update_cache = {
    "latest_version": None,
    "download_url": None,
    "release_url": None,
    "checked": False,
}


def _parse_version(v):
    """Parse version string like '1.2.3' into tuple (1, 2, 3)."""
    v = v.lstrip("v")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _get_platform_asset_suffix():
    """Return the expected installer file suffix for the current platform."""
    if sys.platform == "win32":
        return ".exe"
    elif sys.platform == "darwin":
        return "-macOS.dmg"
    return None


def _match_asset(name):
    """Check if a release asset name matches the current platform."""
    suffix = _get_platform_asset_suffix()
    if not suffix:
        return False
    name_lower = name.lower()
    if sys.platform == "win32":
        return name.endswith(".exe") and "setup" in name_lower
    elif sys.platform == "darwin":
        return name.endswith(".dmg") and "macos" in name_lower
    return False


def check_for_update():
    """Check GitHub Releases for a newer version. Returns update info dict."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = Request(url, headers={"Accept": "application/vnd.github.v3+json"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        tag = data.get("tag_name", "")
        latest = tag.lstrip("v")

        if not latest:
            return {"update_available": False}

        current = _parse_version(BRIDGE_VERSION)
        remote = _parse_version(latest)

        if remote <= current:
            _update_cache["checked"] = True
            _update_cache["latest_version"] = None
            _update_cache["download_url"] = None
            _update_cache["release_url"] = None
            return {"update_available": False}

        # Find the matching installer asset for this platform
        download_url = None
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if _match_asset(name):
                download_url = asset.get("browser_download_url")
                break

        _update_cache["checked"] = True
        _update_cache["latest_version"] = latest
        _update_cache["download_url"] = download_url
        _update_cache["release_url"] = data.get("html_url")

        return {
            "update_available": True,
            "latest_version": latest,
            "current_version": BRIDGE_VERSION,
            "download_url": download_url,
            "release_url": data.get("html_url"),
        }

    except (URLError, json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Update check failed: %s", e)
        return {"update_available": False, "error": str(e)}


def get_cached_update():
    """Return cached update info without making a network request."""
    if not _update_cache["checked"]:
        return None
    if not _update_cache["latest_version"]:
        return {"update_available": False}
    return {
        "update_available": True,
        "latest_version": _update_cache["latest_version"],
        "current_version": BRIDGE_VERSION,
        "download_url": _update_cache["download_url"],
        "release_url": _update_cache["release_url"],
    }


def download_and_run_update(shutdown_callback=None):
    """Download the latest installer and launch it, then signal shutdown."""
    url = _update_cache.get("download_url")
    if not url:
        return {"success": False, "error": "No download URL available"}

    version = _update_cache.get("latest_version", "unknown")

    try:
        logger.info("Downloading update v%s from %s", version, url)
        req = Request(url)
        with urlopen(req, timeout=120) as resp:
            installer_data = resp.read()

        tmp_dir = tempfile.gettempdir()

        if sys.platform == "win32":
            installer_path = os.path.join(tmp_dir, f"ScriptCompilerBridge-Setup-{version}.exe")
            with open(installer_path, "wb") as f:
                f.write(installer_data)

            logger.info("Installer saved to %s, launching...", installer_path)
            subprocess.Popen(
                [installer_path, "/SILENT", "/RESTARTAPPLICATIONS"],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )

        elif sys.platform == "darwin":
            dmg_path = os.path.join(tmp_dir, f"ScriptCompilerBridge-{version}-macOS.dmg")
            with open(dmg_path, "wb") as f:
                f.write(installer_data)

            logger.info("DMG saved to %s, opening...", dmg_path)
            subprocess.Popen(["open", dmg_path])

        else:
            return {"success": False, "error": f"Unsupported platform: {sys.platform}"}

        if shutdown_callback:
            shutdown_callback()

        return {"success": True}

    except Exception as e:
        logger.error("Update download/launch failed: %s", e)
        return {"success": False, "error": str(e)}
