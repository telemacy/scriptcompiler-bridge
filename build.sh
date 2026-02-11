#!/usr/bin/env bash
set -e

echo "=== ScriptCompiler Bridge Build (macOS) ==="
echo

# Check for PyInstaller
if ! command -v pyinstaller &> /dev/null; then
    echo "PyInstaller not found. Install with: pip install pyinstaller"
    exit 1
fi

# Clean previous build
echo "Cleaning previous build..."
rm -rf dist build

# Run PyInstaller
echo
echo "Building with PyInstaller..."
pyinstaller bridge.spec --noconfirm

echo
echo "PyInstaller build complete: dist/ScriptCompilerBridge/"

# Extract version
VERSION=$(python3 -c "from bridge.config import BRIDGE_VERSION; print(BRIDGE_VERSION)")

# Try to create DMG
if command -v create-dmg &> /dev/null; then
    echo
    echo "Creating DMG installer..."
    create-dmg \
        --volname "ScriptCompiler Bridge" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --app-drop-link 425 178 \
        --icon "ScriptCompilerBridge.app" 175 178 \
        "dist/ScriptCompilerBridge-${VERSION}-macOS.dmg" \
        "dist/ScriptCompilerBridge.app" \
    || {
        echo "create-dmg fancy layout failed, falling back to hdiutil..."
        hdiutil create -volname "ScriptCompiler Bridge" \
            -srcfolder "dist/ScriptCompilerBridge" \
            -ov -format UDZO \
            "dist/ScriptCompilerBridge-${VERSION}-macOS.dmg"
    }
    echo
    echo "DMG created: dist/ScriptCompilerBridge-${VERSION}-macOS.dmg"
else
    echo
    echo "create-dmg not found. Install with: brew install create-dmg"
    echo "Falling back to hdiutil..."
    hdiutil create -volname "ScriptCompiler Bridge" \
        -srcfolder "dist/ScriptCompilerBridge" \
        -ov -format UDZO \
        "dist/ScriptCompilerBridge-${VERSION}-macOS.dmg"
    echo "DMG created: dist/ScriptCompilerBridge-${VERSION}-macOS.dmg"
fi

echo
echo "=== Build Complete ==="
