@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo Installing Flask...
venv\Scripts\python.exe -m pip install flask

echo.
echo Starting web interface...
echo Open your browser at: http://localhost:5000
echo.
venv\Scripts\python.exe app.py
