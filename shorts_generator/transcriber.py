"""Local Whisper transcription.

Takes the hosted mp4 URL returned by MuAPI's /youtube-download, streams it to
disk, runs faster-whisper locally, and emits the segment shape expected by the
highlight generator: {duration, segments[start,end,text]}.

Uses faster-whisper instead of openai-whisper: resumable HF downloads, no
SHA256 self-validation loop on a partial file, and faster CPU inference.
"""
import os
import shutil
import tempfile
from typing import Dict, Optional

import requests


_WHISPER_MODEL = None
_WHISPER_MODEL_SIZE = None


def _load_model(model_size: str):
    """Lazy-load faster-whisper. Cached across calls so we only pay model-load cost once."""
    global _WHISPER_MODEL, _WHISPER_MODEL_SIZE
    if _WHISPER_MODEL is not None and _WHISPER_MODEL_SIZE == model_size:
        return _WHISPER_MODEL

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install -U faster-whisper\n"
            "It also needs ffmpeg available on PATH."
        ) from e

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Whisper needs ffmpeg to decode media.\n"
            "  macOS:   brew install ffmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg"
        )

    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "faster-whisper")
    os.makedirs(cache_dir, exist_ok=True)

    print(f"[transcribe] loading faster-whisper model '{model_size}' (first run downloads weights)", flush=True)
    _WHISPER_MODEL = WhisperModel(
        model_size,
        device="cpu",
        compute_type="int8",
        download_root=cache_dir,
    )
    _WHISPER_MODEL_SIZE = model_size
    return _WHISPER_MODEL


def _download_to_temp(media_url: str) -> str:
    suffix = os.path.splitext(media_url.split("?")[0])[1] or ".mp4"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="shorts_src_")
    os.close(fd)
    print(f"[transcribe] downloading source to {path}", flush=True)
    with requests.get(media_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    return path


def transcribe(media_url: str, model_size: str = "base", language: Optional[str] = None) -> Dict:
    """Run local faster-whisper on a hosted media URL. Returns {duration, segments[...]}.

    model_size: tiny | base | small | medium | large-v3 — bigger = more accurate, slower.
    """
    model = _load_model(model_size)
    local_path = _download_to_temp(media_url)
    try:
        print(f"[transcribe] running whisper on {local_path}", flush=True)
        segments_iter, info = model.transcribe(
            local_path,
            language=language,
            beam_size=5,
            vad_filter=True,
        )
        segments = []
        for s in segments_iter:
            segments.append({
                "start": float(s.start),
                "end": float(s.end),
                "text": (s.text or "").strip(),
            })
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass

    duration = float(getattr(info, "duration", 0.0)) or (segments[-1]["end"] if segments else 0.0)
    print(f"[transcribe] {len(segments)} segments, {duration:.0f}s of audio", flush=True)
    return {"duration": duration, "segments": segments}
