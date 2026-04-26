# AI YouTube Shorts Generator — Open-Source Opus Clip Alternative

Turn any long-form YouTube video into multiple viral-ready vertical shorts with one command. Powered end-to-end by [MuAPI](https://muapi.ai) — YouTube download, GPT highlight detection, and smart auto-cropping all run through a single API.

> ### Looking for a fully open-source Opus Clip alternative?
> Check out **[Open-Generative-AI](https://github.com/Anil-matcha/Open-Generative-AI)** — a free, self-hostable Opus Clip alternative that pairs perfectly with this generator. If you want a UI on top of this pipeline, start there.

This is a clean rewrite of [SamurAIGPT/AI-Youtube-Shorts-Generator](https://github.com/SamurAIGPT/AI-Youtube-Shorts-Generator) using the AI clip logic from [ViralVadoo](https://www.vadoo.tv) and the MuAPI platform — no FaceCrop OpenCV pipeline, no Sieve, no managed Whisper backend. Just three API calls plus local Whisper.

---

## Why this exists

Opus Clip is the standard tool for turning podcasts and long videos into shorts, but it is closed-source, expensive, and hides its prompts and ranking logic. This repo is a transparent, hackable alternative:

- **You own the prompts.** The full virality scoring system is in [`shorts_generator/highlights.py`](shorts_generator/highlights.py) — tweak it freely.
- **You own the costs.** Pay-per-call MuAPI pricing instead of monthly seats.
- **You own the output.** All clips returned as direct mp4 URLs.

For an off-the-shelf UI experience, point [Open-Generative-AI](https://github.com/Anil-matcha/Open-Generative-AI) at this backend.

---

## How it works

```
YouTube URL
   │
   ▼
[1] MuAPI /youtube-download   →  hosted source mp4
   │
   ▼
[2] Local Whisper             →  timestamped transcript
   │
   ▼
[3] MuAPI /gpt-5-4            →  ranked viral highlights (with hooks + scores)
   │
   ▼
[4] MuAPI /autocrop           →  vertical 9:16 short for each highlight
   │
   ▼
N viral-ready mp4 URLs
```

The highlight prompt is built from a virality framework that ranks signals — hook moments, emotional peaks, opinion bombs, revelation moments, conflict, quotable one-liners, story peaks, and practical value. Long videos (>30 min) are auto-chunked with overlap so nothing gets missed.

---

## Setup

```bash
git clone <this-repo>
cd ai-youtube-shorts-muapi

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# ffmpeg is required for Whisper
brew install ffmpeg            # macOS
# sudo apt install ffmpeg      # Ubuntu

cp .env.example .env
# edit .env and set MUAPI_API_KEY=...   (get one at https://muapi.ai)
```

---

## Usage

```bash
python main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
    --num-clips 3 \
    --aspect-ratio 9:16 \
    --whisper-model base
```

Output:

```
========================================================================
Source video:  https://cdn.muapi.ai/.../video.mp4
Highlights:    7 candidates → kept top 3
========================================================================

#1  score=92  124.3s → 187.6s
     title:  The one mistake that cost me $50K
     hook:   "Nobody talks about this, but it killed my first startup..."
     clip:   https://cdn.muapi.ai/.../short_1.mp4

#2  score=88  ...
```

### Flags

| Flag | Default | Notes |
|------|---------|-------|
| `--num-clips` | `3` | How many shorts to render |
| `--aspect-ratio` | `9:16` | Any ratio; `9:16` for TikTok/Reels, `1:1` for square |
| `--format` | `720` | Source download resolution: `360` / `480` / `720` / `1080` |
| `--whisper-model` | `base` | `tiny` / `base` / `small` / `medium` / `large` |
| `--language` | auto | Force Whisper language code (e.g. `en`) |
| `--output-json` | — | Write the full result (transcript + all highlights) to a file |

---

## Use it as a library

```python
from shorts_generator import generate_shorts

result = generate_shorts(
    "https://www.youtube.com/watch?v=...",
    num_clips=5,
    aspect_ratio="9:16",
)

for short in result["shorts"]:
    print(short["score"], short["title"], short["clip_url"])
```

---

## Project structure

```
ai-youtube-shorts-muapi/
├── main.py                    CLI entry point
├── requirements.txt
├── .env.example
└── shorts_generator/
    ├── __init__.py
    ├── config.py              loads MUAPI_API_KEY
    ├── muapi.py               generic submit + poll wrapper
    ├── downloader.py          /youtube-download
    ├── transcriber.py         local Whisper
    ├── highlights.py          /gpt-5-4 + virality prompt + chunking + dedupe
    ├── clipper.py             /autocrop
    └── pipeline.py            end-to-end orchestrator
```

---

## Credits

- Original concept: [SamurAIGPT/AI-Youtube-Shorts-Generator](https://github.com/SamurAIGPT/AI-Youtube-Shorts-Generator)
- AI clip / virality logic: [ViralVadoo](https://www.vadoo.tv)
- Underlying APIs: [MuAPI](https://muapi.ai)
- Companion full-stack Opus Clip alternative: **[Open-Generative-AI](https://github.com/Anil-matcha/Open-Generative-AI)**

---

## License

MIT
