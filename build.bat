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

:: Download ffmpeg if not present
if not exist ffmpeg\ffmpeg.exe (
    echo.
    echo Downloading ffmpeg...
    if not exist ffmpeg mkdir ffmpeg
    powershell -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg\ffmpeg.zip' }"
    if %errorlevel% neq 0 (
        echo Failed to download ffmpeg!
        exit /b 1
    )
    echo Extracting ffmpeg...
    powershell -Command "& { $ProgressPreference='SilentlyContinue'; Expand-Archive -Path 'ffmpeg\ffmpeg.zip' -DestinationPath 'ffmpeg\temp' -Force }"
    :: Find ffmpeg.exe inside the extracted folder (it's in a versioned subfolder)
    for /d %%D in (ffmpeg\temp\ffmpeg-*) do (
        copy "%%D\bin\ffmpeg.exe" "ffmpeg\ffmpeg.exe" >nul
    )
    :: Clean up
    rmdir /s /q ffmpeg\temp
    del ffmpeg\ffmpeg.zip
    if not exist ffmpeg\ffmpeg.exe (
        echo Failed to extract ffmpeg!
        exit /b 1
    )
    echo ffmpeg downloaded successfully.
) else (
    echo ffmpeg already present, skipping download.
)

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

:: Extract version from config.py
for /f "tokens=*" %%V in ('python -c "from bridge.config import BRIDGE_VERSION; print(BRIDGE_VERSION)"') do set APP_VERSION=%%V

:: Build installer
echo.
echo Building installer with Inno Setup (v%APP_VERSION%)...
iscc /DMyAppVersion="%APP_VERSION%" installer.iss
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
