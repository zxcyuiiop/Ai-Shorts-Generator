@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM The PySide6 desktop app needs the project's virtualenv (created by install.bat).
if not exist venv\Scripts\pythonw.exe (
    echo [ERROR] Virtual environment not found: venv\Scripts\pythonw.exe
    echo Run install.bat first to create the venv and install dependencies.
    pause
    exit /b 1
)

REM PySide6 comes from requirements-desktop.txt; only install it if missing
REM (keeps offline startups fast and quiet).
venv\Scripts\python.exe -c "import PySide6" 2>nul
if errorlevel 1 (
    echo Installing desktop dependencies...
    venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install desktop dependencies.
        pause
        exit /b 1
    )
)

REM Desktop entry point lives in the repo root (desktop.py).
if not exist desktop.py (
    echo [ERROR] desktop.py not found in %~dp0
    pause
    exit /b 1
)

REM pythonw.exe => no console window. Remove /MIN and use python.exe
REM instead if you want to keep a console for debugging output.
start "AI Shorts Generator Desktop" /MIN venv\Scripts\pythonw.exe desktop.py
endlocal
