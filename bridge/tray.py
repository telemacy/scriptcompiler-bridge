import logging
import os
import signal
import sys

from .config import BRIDGE_VERSION
from .settings import get_video_folders, set_video_folder
from .video_library import invalidate_cache

logger = logging.getLogger(__name__)


def _load_icon_image():
    from PIL import Image

    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(__file__))

    icon_path = os.path.join(base, "favicon.png")
    if os.path.exists(icon_path):
        return Image.open(icon_path)

    return Image.new("RGBA", (64, 64), (46, 125, 246, 255))


def _pick_folder():
    """Open a native Windows folder picker using PowerShell (works from any thread)."""
    import subprocess
    import tempfile

    # Write PowerShell script to a temp file to avoid quoting issues
    ps_code = """
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Select Video Folder"
$d.ShowNewFolderButton = $true
if ($d.ShowDialog() -eq "OK") { Write-Output $d.SelectedPath }
"""
    try:
        ps_file = os.path.join(tempfile.gettempdir(), "sc_folder_pick.ps1")
        with open(ps_file, "w") as f:
            f.write(ps_code)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_file],
            capture_output=True, text=True, timeout=120,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        path = result.stdout.strip()
        return path if path else None
    except Exception as e:
        logger.error("Folder picker failed: %s", e)
        return None


def _get_folder_label():
    """Return the current video folder display text for the tray menu."""
    folders = get_video_folders()
    if folders:
        return f"Video Folder: {folders[0]}"
    return "Video Folder: (not set)"


def run_tray(port, shutdown_callback):
    """Run system tray icon. Must be called on the main thread (Windows requirement)."""
    try:
        import pystray
        from pystray import MenuItem as Item
    except ImportError:
        logger.warning("pystray not installed, running without tray icon")
        return None

    icon_image = _load_icon_image()

    def on_quit(icon, item):
        logger.info("Quit requested from tray")
        icon.stop()
        if shutdown_callback:
            shutdown_callback()
        os._exit(0)

    def on_set_folder(icon, item):
        folder = _pick_folder()
        if folder:
            set_video_folder(folder)
            invalidate_cache()
            logger.info("Video folder set to: %s", folder)
            icon.update_menu()

    def on_refresh(icon, item):
        invalidate_cache()
        logger.info("Video library cache cleared, will rescan on next request")

    menu = pystray.Menu(
        Item(f"ScriptCompiler Bridge v{BRIDGE_VERSION}", lambda: None, enabled=False),
        Item(f"Port: {port}", lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(lambda text: _get_folder_label(), lambda: None, enabled=False),
        Item("Set Video Folder...", on_set_folder),
        Item("Refresh Library", on_refresh),
        pystray.Menu.SEPARATOR,
        Item("Quit", on_quit),
    )

    icon = pystray.Icon(
        name="scriptcompiler-bridge",
        icon=icon_image,
        title=f"ScriptCompiler Bridge (:{port})",
        menu=menu,
    )

    icon.run()
    return icon
