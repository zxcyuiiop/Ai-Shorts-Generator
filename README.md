# AI YouTube Shorts Generator

Turn any long YouTube video or local file into ready-to-publish short clips. The app transcribes the speech, asks an LLM for the most viral moments, cuts the best segments, and lets you polish them in a built-in review panel.

## Desktop app (PySide6)

The project is transitioning to a **new native desktop interface** built with PySide6 (QtWidgets — no QML). It drives the same `shorts_generator` pipeline and covers the basics:

- pick a **YouTube link** or a **local video file**;
- configure **number of clips, clip length, aspect ratio and language**;
- toggle **effects** — karaoke subtitles, blurred-background bars, background music, watermark, silence (pause) cut-out;
- run generation with a **live progress log**, then **review and save** results to `output/`.

### Run it (Windows)

```
run_desktop.bat
```

The launcher reuses the project `venv/` (created by `install.bat`), auto-installs `requirements-desktop.txt` if PySide6 is missing, and starts `desktop.py` via `pythonw.exe` (no console window). Manual start also works:

```
venv\Scripts\python.exe desktop.py
```

### Desktop install

PySide6 needs to be in the venv alongside the regular pipeline dependencies:

```
venv\Scripts\python.exe -m pip install -r requirements-local.txt -r requirements-desktop.txt
```

---

## Web UI (legacy)

The original interface is a **legacy web GUI** (Flask, `app.py`, port 5000) with three pages: `/` Generate, `/history`, `/settings`. It is fully functional and kept as-is, but the native desktop app above is where new UI work happens.

## Features

- **Two pipelines** — fully remote via [MuAPI](https://muapi.ai) (hosted, nothing to install) or fully local: yt-dlp download → faster-whisper transcription → LLM highlight picking (OpenAI / Gemini / Ollama / NVIDIA NIM) → ffmpeg/OpenCV cutting
- **Review-first workflow** — generation produces 16:9 drafts; the GPU-heavy vertical crop, effects and captions run only on clips you actually save
- **Saved under the highlight title** — approving a draft can carry the LLM title; the file lands in `output/saved/` as `<title>.mp4` and the same text is burned ~750px above the bottom of the frame (`TITLE_*` env)
- **Batch save** — «Сохранить всё» in the review header queues every remaining clip through `POST /api/shorts/save_batch`
- **Persistent history** — saved clips are recorded in `output/history.json` with a JPEG thumbnail; browse/star/delete them on the `/history` page (survives restarts)
- **Effects** — blurred-background fit, TikTok-style watermark (position/scale grid), background music with volume control, silence jump-cuts
- **Custom watermark pause** — upload any PNG/JPEG (`WATERMARK_FILE`) and the clip freeze-frames on it for `WATERMARK_DURATION_SEC` at `WATERMARK_AT_SEC` during save
- **Karaoke captions** — word-level burned-in subtitles with a highlight on the current word (opt-in; classic style available)
- **Face tracking** — OpenCV-based reframe keeps the speaker in frame during the vertical crop (env switchable)
- **Cover thumbnails** — one click grabs a representative frame and overlays the title as a JPEG
- **Saved / discard flow** — «Сохранить» applies everything and moves the clip to `output/saved/`; «Удалить» removes it
- **Web GUI** (legacy, Flask, port 5000) split into three pages: `/` Generate, `/history`, `/settings` (API keys + effect knobs)
- **Desktop GUI** — new native PySide6 interface (see "Desktop app (PySide6)" above)
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

Then launch the legacy web UI:

```
start_gui.bat
```

(For the new native interface, run `run_desktop.bat` instead — see "Desktop app (PySide6)" at the top.)

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
