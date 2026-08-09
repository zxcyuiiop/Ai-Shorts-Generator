"""End-to-end orchestrator.

Two modes:
  * mode="api"   (default) — MuAPI does download / transcribe / LLM / autocrop.
                              Fast, no local deps, pay-per-call.
  * mode="local"            — yt-dlp + faster-whisper + OpenAI or Gemini + ffmpeg/opencv.
                              Self-hosted, LLM_PROVIDER selects OpenAI or Gemini.
"""
import os
from typing import Dict, List, Optional

from .clipper import crop_highlights
from .downloader import download_youtube
from .highlights import call_muapi_llm, get_highlights
from .transcriber import transcribe


def _run_local(
    youtube_url: str,
    num_clips: int,
    aspect_ratio: str,
    download_format: str,
    language: Optional[str],
    llm_provider: Optional[str] = None,
    clip_length: Optional[str] = None,
) -> Dict:
    from .local.clipper import crop_highlights_local
    from .local.downloader import download_youtube_local, get_last_download_info
    from .local.llm import make_local_llm_fn
    from .local.transcriber import transcribe_local

    source_path = download_youtube_local(youtube_url, fmt=download_format)

    transcript = transcribe_local(source_path, language=language)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    llm_fn = make_local_llm_fn(llm_provider)
    highlights_result = get_highlights(
        transcript, num_clips=num_clips, llm_fn=llm_fn, clip_length=clip_length
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    print(f"[pipeline/local] cropping {len(top)} of {len(all_highlights)} candidates", flush=True)

    # Save this run's shorts in a subfolder named after the video (its title for
    # URLs, the file stem for local inputs). crop_highlights_local gained an
    # output_dir kwarg across this change, so call it defensively: fall back to
    # the flat output dir if the installed clipper doesn't accept it yet.
    from .config import LOCAL_OUTPUT_DIR
    subfolder = (get_last_download_info().get("folder") or "").strip() or "video"
    shorts_dir = os.path.join(LOCAL_OUTPUT_DIR, subfolder)
    # Drafts only, in the SOURCE's horizontal framing (16:9), no target-aspect
    # reframe at render time: the vertical crop is destructive, so it happens
    # on save (POST /api/shorts/save -> _reframe_vertical with face tracking ->
    # finalize_clip_local for blur/overlay/music), after the user approves a
    # short in the review panel. Saves GPU on rejected clips.
    # task4: transcript is threaded through so crop_highlights_local can burn
    # karaoke captions at finalize time without re-transcribing.
    shorts = crop_highlights_local(
        source_path, top, aspect_ratio="16:9", output_dir=shorts_dir,
        finalize=False, transcript=transcript,
    )
    for short in shorts:
        short["draft_aspect"] = "16:9"          # what the draft was rendered at
        short["target_aspect"] = aspect_ratio   # what save will reframe it to

    return {
        "mode": "local",
        "source_video_url": source_path,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
    }


def _run_api(
    youtube_url: str,
    num_clips: int,
    aspect_ratio: str,
    download_format: str,
    language: Optional[str],
    clip_length: Optional[str] = None,
) -> Dict:
    source_url = download_youtube(youtube_url, fmt=download_format)

    transcript = transcribe(source_url, language=language)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(
        transcript, num_clips=num_clips, llm_fn=call_muapi_llm, clip_length=clip_length
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    print(f"[pipeline] cropping {len(top)} of {len(all_highlights)} candidates", flush=True)

    shorts = crop_highlights(source_url, top, aspect_ratio=aspect_ratio)

    return {
        "mode": "api",
        "source_video_url": source_url,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
    }


def generate_shorts(
    youtube_url: str,
    num_clips: int = 3,
    aspect_ratio: str = "9:16",
    download_format: str = "720",
    language: Optional[str] = None,
    mode: str = "api",
    llm_provider: Optional[str] = None,
    clip_length: Optional[str] = None,
) -> Dict:
    """Run the full pipeline and return a structured result.

    Args:
        youtube_url: source URL.
        num_clips: how many shorts to render.
        aspect_ratio: e.g. "9:16", "1:1".
        download_format: source resolution ("360" / "480" / "720" / "1080").
        language: ISO-639-1 to force Whisper language detection.
        mode: "api" (default, MuAPI) or "local" (yt-dlp + faster-whisper +
            OpenAI or Gemini + ffmpeg).
        llm_provider: local mode only — "openai" / "gemini" / "ollama" / "nim".
            Defaults to the LLM_PROVIDER env var.
        clip_length: target clip duration — "any" (default), "short" (<30s),
            "medium" (30-60s), "long" (60-90s) or "extended" (90-180s).

    Returns:
        {
          "mode": "api" | "local",
          "source_video_url": str,   # hosted URL (api) or local path (local)
          "transcript": {...},
          "highlights": [...],       # all candidates ranked
          "shorts": [...],           # top `num_clips` with clip_url / local path
        }
    """
    mode = (mode or "api").lower()
    if mode == "local":
        return _run_local(youtube_url, num_clips, aspect_ratio, download_format,
                          language, llm_provider, clip_length)
    if mode == "api":
        return _run_api(youtube_url, num_clips, aspect_ratio, download_format,
                        language, clip_length)
    raise ValueError(f"Unknown mode: {mode!r}. Use 'api' or 'local'.")
