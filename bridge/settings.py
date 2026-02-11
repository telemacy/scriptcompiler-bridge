import json
import os
import logging
from pathlib import Path

from .config import SETTINGS_DIR_NAME, SETTINGS_FILE_NAME

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "video_folders": [],
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


