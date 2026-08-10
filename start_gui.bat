@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8

REM The Flask GUI needs the project's virtualenv (created by install.bat).
if not exist venv\Scripts\python.exe (
    echo [ERROR] Virtual environment not found. Run install.bat first.
    pause
    exit /b 1
)

REM Flask is part of requirements.txt; only install it if it is missing
REM (keeps offline startups fast and quiet).
venv\Scripts\python.exe -c "import flask" 2>nul
if errorlevel 1 (
    echo Installing Flask...
    venv\Scripts\python.exe -m pip install flask
)

echo.
echo Starting web interface...
echo Open your browser at: http://localhost:5000
echo.
venv\Scripts\python.exe app.py
