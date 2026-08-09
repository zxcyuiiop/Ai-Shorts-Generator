#!/usr/bin/env bash
# AI YouTube Shorts Generator — installer (Linux / macOS)
set -e
export PYTHONIOENCODING=utf-8

echo ""
echo "=== AI YouTube Shorts Generator — installer ==="
echo ""

# --- 1. Python (3.10+) ---
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        ver=$("$c" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        major=${ver%%.*}
        minor=${ver##*.}
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ] 2>/dev/null; then
            PY="$c"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] Python 3.10+ not found. Install it via your package manager and re-run."
    exit 1
fi
echo "Found $PY ($($PY --version 2>&1))"

# --- 2. ffmpeg (required for local mode) ---
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo ""
    echo "[WARN] ffmpeg not found on PATH — local mode will fail."
    echo "       Install it (e.g. sudo apt install ffmpeg / brew install ffmpeg) and re-run."
    echo "       (API mode works without it.)"
fi

# --- 3. Virtual environment ---
if [ ! -x venv/bin/python ]; then
    echo ""
    echo "Creating virtual environment in ./venv ..."
    "$PY" -m venv venv || { echo "[ERROR] venv creation failed — install python3-venv."; exit 1; }
else
    echo "Virtual environment already exists — reusing it."
fi
VPY=venv/bin/python

# --- 4. Dependencies ---
echo ""
echo "Installing dependencies (this can take a few minutes)..."
"$VPY" -m pip install --upgrade pip
if ! "$VPY" -m pip install -r requirements-local.txt; then
    echo ""
    echo "[WARN] Full install failed (often a GPU wheel issue on unusual hardware)."
    echo "       Falling back to the minimal API-mode stack..."
    "$VPY" -m pip install -r requirements.txt
fi

# --- 5. .env ---
if [ ! -f .env ] && [ -f .env.example ]; then
    echo ""
    echo "Creating .env from .env.example — fill in your API keys before running."
    cp .env.example .env
fi

echo ""
echo "=== Done! ==="
echo "Start the web UI with:  ./start_gui.sh"
echo "Or the CLI with:        venv/bin/python main.py --help"
echo ""
