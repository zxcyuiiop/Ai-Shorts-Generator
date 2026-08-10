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

### 1. Grab the code

```
git clone https://github.com/zxcyuiiop/Ai-Shorts-Generator.git
cd Ai-Shorts-Generator
```

### 2. Requirements

- **Python 3.10+** (both installers check this).
- **ffmpeg + ffprobe** on `PATH` — required for local mode and clip saving. The `install.sh`/`install.bat` scripts warn if it is missing; install via your package manager (`sudo apt install ffmpeg`, `brew install ffmpeg`, or a Windows build).
- An **NVIDIA GPU** is optional — the pipeline auto-detects `h264_nvenc`/`hevc_nvenc` and falls back to CPU encoding (`FFMPEG_ENCODER`, `FORCE_CPU_FFMPEG` env vars).

### 3. Install + run

#### Windows

Double-click `install.bat` (or run it from a terminal). It checks Python 3.10+, creates a local `.venv`-style `venv/`, installs everything, and can optionally copy `.env.example` → `.env` so you can fill in your API keys.

Then launch the web UI:

```
start_gui.bat
```

Open http://localhost:5000 in your browser.
The launcher refuses to start if `venv/` is missing and tells you to run `install.bat` first — it never silently installs dependencies outside the venv.

#### Linux / macOS

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

### "Sign in to confirm you're not a bot"

YouTube sometimes blocks anonymous downloads. Fix it one of three ways (documented in `.env.example`):

- **Browser cookies** — add `YTDLP_COOKIES_FROM_BROWSER=edge` (or `chrome` / `firefox` / `brave`) to `.env`, **fully close that browser**, and retry. yt-dlp will read the session directly.
- **Cookies file** — export cookies with the "Get cookies.txt LOCALLY" browser extension and set `YTDLP_COOKIES=C:\path\to\cookies.txt` in `.env`.
- **Skip YouTube** — download the video yourself and pick it via the "Local file" field in the GUI; the pipeline then never talks to YouTube.

## Configuration

Copy `.env.example` to `.env` and fill in the keys you need. All effects (blur, watermark, silence cuts, music, captions, thumbnails) are env-gated and documented there; per-request overrides are also exposed in the GUI settings panel (persisted to `settings.local.json`).

**Watermark asset.** The TikTok-style video watermark (`OVERLAY_ENABLED=1`) needs a `TIKTOK1.mov` file in the repo root, and that file is **not** shipped in the repository (it's a ~30 MB asset). Drop your own transparent-background `.mov` overlay there, or turn the watermark off with `OVERLAY_ENABLED=0`.

**GUI server.** `app.py` binds to `0.0.0.0:5000` by default so the panel is reachable from other devices on your LAN — anyone on the network can use it. To restrict it to this machine set `GUI_HOST=127.0.0.1`; to require an access token for all `/api/*` calls set `GUI_TOKEN=<secret>` and pass it in the `X-Api-Token` header or `?token=` query param (the web UI picks it up automatically).

## Documentation

- [`GUI_README.md`](GUI_README.md) — full GUI guide (in Russian): review flow, effects knobs, API endpoints, troubleshooting.
- [`OLLAMA_NIM_GUIDE.md`](OLLAMA_NIM_GUIDE.md) — using a local Ollama or NVIDIA NIM endpoint as the highlight-picking LLM.
- [`LICENSE`](LICENSE) — MIT.

## Tests

Run every suite (stubbed, no heavyweight deps needed):

```
venv/Scripts/python.exe run_all_tests.py
```

## License

MIT — see `LICENSE`.
