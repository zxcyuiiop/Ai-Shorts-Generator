#!/bin/bash
set -e
export PYTHONIOENCODING=utf-8

# The Flask GUI needs the project's virtualenv (created by install.sh).
if [ ! -x venv/bin/python ]; then
    echo "[ERROR] Virtual environment not found. Run ./install.sh first."
    exit 1
fi

# Flask is part of requirements.txt; only install it if it is missing
# (keeps offline startups fast and quiet).
if ! venv/bin/python -c "import flask" 2>/dev/null; then
    echo "Installing Flask..."
    venv/bin/python -m pip install flask
fi

echo ""
echo "Starting web interface..."
echo "Open your browser at: http://localhost:5000"
echo ""
venv/bin/python app.py
