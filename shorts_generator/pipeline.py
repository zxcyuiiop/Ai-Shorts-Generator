"""End-to-end orchestrator.

YouTube URL  →  MuAPI download  →  local Whisper transcript
             →  MuAPI gpt-5-4 highlights  →  MuAPI autocrop clips
"""
from typing import Dict, List, Optional

from .clipper import crop_highlights
from .downloader import download_youtube
from .highlights import get_highlights
from .transcriber import transcribe


def generate_shorts(
    youtube_url: str,
    num_clips: int = 3,
    aspect_ratio: str = "9:16",
    download_format: str = "720",
    whisper_model: str = "base",
    language: Optional[str] = None,
) -> Dict:
    """Run the full pipeline and return a structured result.

    Returns:
        {
          "source_video_url": str,   # MuAPI-hosted mp4
          "transcript": {...},
          "highlights": [...],       # all candidates ranked
          "shorts": [...],           # top `num_clips` with clip_url
        }
    """
    source_url = download_youtube(youtube_url, fmt=download_format)

    transcript = transcribe(source_url, model_size=whisper_model, language=language)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(transcript)
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    print(f"[pipeline] cropping {len(top)} of {len(all_highlights)} candidates", flush=True)

    shorts = crop_highlights(source_url, top, aspect_ratio=aspect_ratio)

    return {
        "source_video_url": source_url,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
    }
