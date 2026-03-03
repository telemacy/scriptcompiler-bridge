import os
import sys
import logging

logger = logging.getLogger(__name__)


def get_ytdlp_path():
    """Return the path to the bundled yt-dlp binary."""
    if getattr(sys, 'frozen', False):
        bundle_dir = sys._MEIPASS
    else:
        bundle_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    ytdlp_dir = os.path.join(bundle_dir, 'yt-dlp')
    ytdlp_name = 'yt-dlp.exe' if sys.platform == 'win32' else 'yt-dlp'
    ytdlp_path = os.path.join(ytdlp_dir, ytdlp_name)

    if os.path.isfile(ytdlp_path):
        logger.info("Found bundled yt-dlp at: %s", ytdlp_path)
        return ytdlp_path

    import shutil
    system_path = shutil.which('yt-dlp')
    if system_path:
        logger.info("Using system yt-dlp at: %s", system_path)
        return system_path

    return None
