# AI YouTube Shorts Generator

Turn any long YouTube video or local file into ready-to-publish short clips. The app transcribes the speech, asks an LLM for the most viral moments, cuts the best segments, and lets you polish them in a built-in review panel.

## Features

- **Two pipelines** — fully remote via [MuAPI](https://muapi.ai) (hosted, nothing to install) or fully local: yt-dlp download → faster-whisper transcription → LLM highlight picking (OpenAI / Gemini / Ollama / NVIDIA NIM) → ffmpeg/OpenCV cutting
- **Review-first workflow** — generation produces 16:9 drafts; the GPU-heavy vertical crop, effects and captions run only on clips you actually save
- **Effects** — blurred-background fit, TikTok-style watermark (position/scale grid), background music with volume control, silence jump-cuts
- **Karaoke captions** — word-level burned-in subtitles with a highlight on the current word (opt-in; classic style available)
- **Face tracking** — OpenCV-based reframe keeps the speaker in frame during the vertical crop (env switchable)
- **Cover thumbnails** — one click grabs a representative frame and overlays the title as a JPEG
- **Saved / discard flow** — «Сохранить» applies everything and moves the clip to `output/saved/`; «Удалить» removes it
- **Web GUI** (Flask, port 5000) with live progress, settings persistence, and per-clip review cards
- **CLI** — same pipeline from the command line for scripting

## Quick start

### Windows

Double-click `install.bat` (or run it from a terminal). It checks Python 3.10+, creates a local `.venv`-style `venv/`, installs everything, and can optionally copy `.env.example` → `.env` so you can fill in your API keys.

Then launch the web UI:

```
start_gui.bat
```

Open http://localhost:5000 in your browser.

### Linux / macOS

```
chmod +x install.sh start_gui.sh
./install.sh
./start_gui.sh
```

(The installer warns if `ffmpeg` is missing — install it via your package manager first.)

### CLI

```
# API mode (needs MUAPI_API_KEY in .env)
venv/Scripts/python.exe main.py "https://www.youtube.com/watch?v=..." --num-clips 5

# Local mode (needs an LLM provider key in .env, e.g. OPENAI_API_KEY)
venv/Scripts/python.exe main.py "https://www.youtube.com/watch?v=..." --mode local --aspect-ratio 9:16
```

## Configuration

Copy `.env.example` to `.env` and fill in the keys you need. All effects (blur, watermark, music, captions, thumbnails) are env-gated and documented there; per-request overrides are also exposed in the GUI settings panel (`settings.local.json`).

## Tests

Run every suite (stubbed, no heavyweight deps needed):

```
venv/Scripts/python.exe run_all_tests.py
```

## License

MIT — see `LICENSE`.
