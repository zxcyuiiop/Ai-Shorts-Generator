@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
set PYTHONIOENCODING=utf-8

echo.
echo === AI YouTube Shorts Generator — installer (Windows) ===
echo.

REM --- 1. Python (3.10+) ---
set PY=
where python >nul 2>&1 && set PY=python
if not defined PY (
    where py >nul 2>&1 && set PY=py -3
)
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo         and re-run this script. On the installer tick "Add Python to PATH".
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('!PY! --version 2^>^&1') do set PYVER=%%v
echo Found Python !PYVER!

REM --- 2. Virtual environment ---
if not exist venv\Scripts\python.exe (
    echo.
    echo Creating virtual environment in .\venv ...
    !PY! -m venv venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed. You may need to install the "python3-venv" package or re-run as admin.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists — reusing it.
)

set VPY=venv\Scripts\python.exe

REM --- 3. Dependencies ---
echo.
echo Installing dependencies (this can take a few minutes)...
!VPY! -m pip install --upgrade pip
if errorlevel 1 goto :pip_fail

REM Full local-mode stack: pipeline + ffmpeg helpers + GPU libs for whisper.
!VPY! -m pip install -r requirements-local.txt
if errorlevel 1 (
    echo.
    echo [WARN] Full install failed (often a GPU wheel issue on unusual hardware).
    echo        Falling back to the minimal API-mode stack...
    !VPY! -m pip install -r requirements.txt
    if errorlevel 1 goto :pip_fail
)

REM --- 4. .env ---
if not exist .env (
    if exist .env.example (
        echo.
        echo Creating .env from .env.example — fill in your API keys before running.
        copy /y .env.example .env >nul
    )
)

echo.
echo === Done! ===
echo Start the web UI with:  start_gui.bat
echo Or the CLI with:        venv\Scripts\python.exe main.py --help
echo.
pause
exit /b 0

:pip_fail
echo [ERROR] Dependency installation failed — check your network connection and Python version.
pause
exit /b 1
