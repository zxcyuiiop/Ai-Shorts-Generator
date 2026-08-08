#!/bin/bash
export PYTHONIOENCODING=utf-8

echo "Installing Flask..."
venv/bin/python -m pip install flask

echo ""
echo "Starting web interface..."
echo "Open your browser at: http://localhost:5000"
echo ""
venv/bin/python app.py
