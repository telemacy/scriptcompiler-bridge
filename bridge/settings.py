import json
import os
import logging
import sys
from pathlib import Path

from .config import SETTINGS_DIR_NAME, SETTINGS_FILE_NAME

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "video_folders": [],
    "yt_dlp_quality": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    "autostart": False,
}


def get_settings_path():
    return Path.home() / SETTINGS_DIR_NAME / SETTINGS_FILE_NAME


def load_settings():
    path = get_settings_path()
    if not path.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        return merged
    except Exception as e:
        logger.warning("Failed to load settings from %s: %s", path, e)
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        logger.error("Failed to save settings to %s: %s", path, e)


def get_video_folders():
    return load_settings().get("video_folders", [])


def add_video_folder(folder):
    folder = os.path.normpath(folder)
    settings = load_settings()
    folders = settings.get("video_folders", [])
    if folder not in folders:
        folders.append(folder)
        settings["video_folders"] = folders
        save_settings(settings)
    return folders


def set_video_folder(folder):
    folder = os.path.normpath(folder)
    settings = load_settings()
    settings["video_folders"] = [folder]
    save_settings(settings)
    return [folder]


def get_settings():
    settings = load_settings()
    # Always read autostart from OS to stay in sync
    settings["autostart"] = _get_autostart()
    return settings


def update_settings(updates: dict):
    allowed_keys = set(DEFAULT_SETTINGS.keys())
    filtered = {k: v for k, v in updates.items() if k in allowed_keys}

    # Handle autostart toggle separately
    if "autostart" in filtered:
        enabled = bool(filtered["autostart"])
        _set_autostart(enabled)
        filtered["autostart"] = enabled

    settings = load_settings()
    settings.update(filtered)
    save_settings(settings)

    # Sync autostart state from OS (in case it was set externally)
    settings["autostart"] = _get_autostart()
    return settings


def _get_app_executable():
    """Get the path to the bridge executable."""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return None


def _get_autostart():
    """Check if autostart is currently enabled in the OS."""
    if sys.platform == 'win32':
        return _get_autostart_windows()
    elif sys.platform == 'darwin':
        return _get_autostart_macos()
    return False


def _set_autostart(enabled):
    """Enable or disable autostart in the OS."""
    if sys.platform == 'win32':
        _set_autostart_windows(enabled)
    elif sys.platform == 'darwin':
        _set_autostart_macos(enabled)


def _get_autostart_windows():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ
        )
        try:
            winreg.QueryValueEx(key, "ScriptCompilerBridge")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _set_autostart_windows(enabled):
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        try:
            if enabled:
                exe_path = _get_app_executable()
                if exe_path:
                    winreg.SetValueEx(key, "ScriptCompilerBridge", 0, winreg.REG_SZ, f'"{exe_path}"')
                    logger.info("Autostart enabled: %s", exe_path)
                else:
                    logger.warning("Cannot enable autostart: not running as frozen executable")
            else:
                try:
                    winreg.DeleteValue(key, "ScriptCompilerBridge")
                    logger.info("Autostart disabled")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        logger.error("Failed to set autostart: %s", e)


def _get_autostart_macos():
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.scriptcompiler.bridge.plist"
    return plist_path.exists()


def _set_autostart_macos(enabled):
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.scriptcompiler.bridge.plist"
    if enabled:
        exe_path = _get_app_executable()
        if not exe_path:
            logger.warning("Cannot enable autostart: not running as frozen executable")
            return
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.scriptcompiler.bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>"""
        try:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist_content)
            logger.info("Autostart enabled (macOS): %s", plist_path)
        except Exception as e:
            logger.error("Failed to create launch agent: %s", e)
    else:
        try:
            plist_path.unlink(missing_ok=True)
            logger.info("Autostart disabled (macOS)")
        except Exception as e:
            logger.error("Failed to remove launch agent: %s", e)


