from concurrent.futures import ThreadPoolExecutor

BRIDGE_VERSION = "1.1.1"
BRIDGE_NAME = "ScriptCompiler Bridge"
GITHUB_REPO = "telemacy/scriptcompiler-bridge"

DEFAULT_PORT = 9876
DEFAULT_HOST = "127.0.0.1"

CORS_ALLOW_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://(.*\.)?scriptcompiler\.com$"

VIDEO_EXTENSIONS = ["mp4", "webm", "mkv", "avi", "mov", "wmv", "flv", "m4v"]
AUDIO_EXTENSIONS = ["mp3", "wav", "ogg", "flac", "aac", "m4a", "wma", "opus"]
FUNSCRIPT_EXTENSIONS = ["funscript", "json"]

TRACKING_COMMAND_TIMEOUT = 5.0
SCENE_DETECT_TIMEOUT = 120.0
AUDIO_ANALYSIS_TIMEOUT = 120.0

SETTINGS_DIR_NAME = ".scriptcompiler-bridge"
SETTINGS_FILE_NAME = "settings.json"

# Shared executor for blocking I/O (file dialogs, scene detection, etc.)
EXECUTOR = ThreadPoolExecutor(max_workers=2)

