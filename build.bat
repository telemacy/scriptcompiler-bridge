@echo off
setlocal

echo === ScriptCompiler Bridge Build ===
echo.

:: Check for PyInstaller
where pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo PyInstaller not found. Install with: pip install pyinstaller
    exit /b 1
)

:: Clean previous build
echo Cleaning previous build...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

:: Run PyInstaller
echo.
echo Building with PyInstaller...
pyinstaller bridge.spec --noconfirm
if %errorlevel% neq 0 (
    echo PyInstaller build failed!
    exit /b 1
)

echo.
echo PyInstaller build complete: dist\ScriptCompilerBridge\

:: Check for Inno Setup
where iscc >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo Inno Setup compiler (iscc) not found in PATH.
    echo Install Inno Setup from https://jrsoftware.org/issetup.php
    echo Then run: iscc installer.iss
    echo.
    echo Skipping installer creation.
    goto :done
)

:: Build installer
echo.
echo Building installer with Inno Setup...
iscc installer.iss
if %errorlevel% neq 0 (
    echo Installer build failed!
    exit /b 1
)

echo.
echo Installer created: dist\ScriptCompilerBridge-Setup-*.exe

:done
echo.
echo === Build Complete ===
endlocal
