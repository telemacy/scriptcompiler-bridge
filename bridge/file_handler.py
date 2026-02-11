import asyncio
import os
import logging

from .config import VIDEO_EXTENSIONS, FUNSCRIPT_EXTENSIONS, EXECUTOR

logger = logging.getLogger(__name__)


def _tk_open_file(title, filetypes):
    """Open a native file dialog using tkinter (must run in a thread)."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()

    path = filedialog.askopenfilename(title=title, filetypes=filetypes)

    root.destroy()
    return path if path else None


def _tk_save_file(title, filetypes, default_name):
    """Open a native save dialog using tkinter (must run in a thread)."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()

    path = filedialog.asksaveasfilename(
        title=title,
        filetypes=filetypes,
        defaultextension=filetypes[0][1] if filetypes else ".funscript",
        initialfile=default_name,
    )

    root.destroy()
    return path if path else None


async def open_video_dialog():
    """Open a native file dialog for video files."""
    ext_pattern = " ".join(f"*.{ext}" for ext in VIDEO_EXTENSIONS)
    filetypes = [("Video Files", ext_pattern), ("All Files", "*.*")]

    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(EXECUTOR, _tk_open_file, "Open Video", filetypes)

    if not path:
        return None

    return {
        "path": path,
        "name": os.path.basename(path),
    }


async def open_funscript_dialog():
    """Open a native file dialog for funscript/JSON files."""
    ext_pattern = " ".join(f"*.{ext}" for ext in FUNSCRIPT_EXTENSIONS)
    filetypes = [("Funscript Files", ext_pattern), ("All Files", "*.*")]

    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(EXECUTOR, _tk_open_file, "Open Funscript", filetypes)

    if not path:
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error("Failed to read funscript file: %s", e)
        return {"path": path, "name": os.path.basename(path), "error": str(e)}

    return {
        "path": path,
        "name": os.path.basename(path),
        "content": content,
    }


async def save_funscript_dialog(data, default_name="script.funscript"):
    """Save funscript data via native save dialog."""
    filetypes = [("Funscript Files", "*.funscript"), ("JSON Files", "*.json"), ("All Files", "*.*")]

    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(EXECUTOR, _tk_save_file, "Save Funscript", filetypes, default_name)

    if not path:
        return None

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception as e:
        logger.error("Failed to save funscript file: %s", e)
        return {"path": path, "name": os.path.basename(path), "error": str(e)}

    return {
        "path": path,
        "name": os.path.basename(path),
    }


async def write_funscript(data, path):
    """Write funscript data directly to a given path (no dialog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception as e:
        logger.error("Failed to write funscript file: %s", e)
        return {"success": False, "path": path, "error": str(e)}

    return {
        "success": True,
        "path": path,
        "name": os.path.basename(path),
    }
