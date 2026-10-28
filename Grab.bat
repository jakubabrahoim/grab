@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Grab...
  python -m venv .venv || (echo Python 3 is required: https://www.python.org/downloads/ & pause & exit /b 1)
)

rem Keep yt-dlp current - YouTube changes often and old versions break.
echo Checking for downloader updates...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check --upgrade "yt-dlp[default]" >nul 2>&1

where ffmpeg >nul 2>&1 || if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*" (
  echo Installing ffmpeg ^(needed for MP3 and HD video^)...
  winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements
)

".venv\Scripts\python.exe" app.py
