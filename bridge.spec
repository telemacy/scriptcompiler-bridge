# PyInstaller spec for ScriptCompiler Bridge
# Builds two executables: main bridge app + tracker subprocess
# Usage: pyinstaller bridge.spec

import sys
import os

block_cipher = None
is_mac = sys.platform == 'darwin'

# Locate ffmpeg binary to bundle
_ffmpeg_binaries = []
_ffmpeg_name = 'ffmpeg.exe' if not is_mac else 'ffmpeg'
_ffmpeg_path = os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'ffmpeg', _ffmpeg_name)
if os.path.isfile(_ffmpeg_path):
    _ffmpeg_binaries.append((_ffmpeg_path, 'ffmpeg'))
    print(f"Bundling ffmpeg from: {_ffmpeg_path}")
else:
    print(f"WARNING: ffmpeg not found at {_ffmpeg_path} - audio analysis may not work")

# Packages installed globally but NOT needed by the bridge
_global_excludes = [
    'torch', 'torchaudio', 'torchvision', 'pytorch_lightning', 'torchmetrics',
    'tensorflow', 'keras', 'tensorboard',
    'transformers', 'sentence_transformers', 'huggingface_hub', 'tokenizers', 'safetensors',
    'datasets', 'accelerate',
    'onnxruntime', 'onnx',
    'pandas', 'matplotlib', 'plotly', 'seaborn',
    'mediapipe',
    'lxml', 'beautifulsoup4', 'bs4',
    'jupyter', 'jupyter_client', 'jupyter_core', 'jupyterlab', 'notebook', 'nbformat', 'nbconvert',
    'IPython', 'ipykernel', 'ipywidgets',
    'sympy', 'networkx',
    'Crypto', 'cryptography',
    'psutil',
    'jedi', 'parso',
    'pytest', 'unittest',
    'setuptools', 'pip', 'wheel',
]

# --- Tracker subprocess (headless, no console window) ---
tracker_a = Analysis(
    ['tracker.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['numpy', 'cv2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pystray', 'PIL', 'fastapi', 'uvicorn'] + _global_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
tracker_pyz = PYZ(tracker_a.pure, tracker_a.zipped_data, cipher=block_cipher)
tracker_exe = EXE(
    tracker_pyz,
    tracker_a.scripts,
    [],
    exclude_binaries=True,
    name='tracker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not is_mac,
    console=False,
    icon=None,
)

# --- Main bridge app ---
bridge_hidden = [
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'PIL.Image',
    # librosa + audio analysis dependencies
    'librosa',
    'librosa.core',
    'librosa.beat',
    'librosa.onset',
    'librosa.feature',
    'librosa.segment',
    'librosa.util',
    'soundfile',
    'audioread',
    'sklearn',
    'sklearn.cluster',
    'scipy',
    'scipy.ndimage',
    'scipy.signal',
    'scipy.fft',
    'scipy.sparse',
]

if is_mac:
    bridge_hidden.append('pystray._darwin')
else:
    bridge_hidden.append('pystray._win32')

bridge_a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_ffmpeg_binaries,
    datas=[('favicon.png', '.')],
    hiddenimports=bridge_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_global_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
bridge_pyz = PYZ(bridge_a.pure, bridge_a.zipped_data, cipher=block_cipher)
bridge_exe = EXE(
    bridge_pyz,
    bridge_a.scripts,
    [],
    exclude_binaries=True,
    name='ScriptCompilerBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not is_mac,
    console=False,
    icon='icon.ico' if not is_mac else None,
    version_info=None,
)

# --- Merge into single output folder ---
coll = COLLECT(
    bridge_exe,
    bridge_a.binaries,
    bridge_a.zipfiles,
    bridge_a.datas,
    tracker_exe,
    tracker_a.binaries,
    tracker_a.zipfiles,
    tracker_a.datas,
    strip=False,
    upx=not is_mac,
    upx_exclude=[],
    name='ScriptCompilerBridge',
)

# On macOS, also create a .app bundle
if is_mac:
    app = BUNDLE(
        coll,
        name='ScriptCompilerBridge.app',
        icon=None,
        bundle_identifier='com.scriptcompiler.bridge',
        info_plist={
            'LSUIElement': True,  # Hide from Dock (menu bar app)
        },
    )
