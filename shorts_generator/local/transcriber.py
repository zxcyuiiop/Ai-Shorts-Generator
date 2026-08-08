"""Local transcription via faster-whisper.

Reads a local media file and returns the same shape the highlight generator
expects: {duration, segments[start, end, text]}.
"""
import os
import re
from pathlib import Path
from typing import Dict, Optional

from ..config import env


def _transcript_cache_path(media_path: str) -> Path:
    """Return the .srt cache path for a media file."""
    cache_dir = Path(env("LOCAL_OUTPUT_DIR", "output"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / (Path(media_path).stem + ".srt")


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + (millis / 1000.0)


def _write_srt_cache(media_path: str, transcript: Dict) -> Path:
    cache_path = _transcript_cache_path(media_path)
    lines = []
    for idx, segment in enumerate(transcript.get("segments", []), start=1):
        start = _format_srt_timestamp(float(segment["start"]))
        end = _format_srt_timestamp(float(segment["end"]))
        text = str(segment.get("text", "")).strip().replace("\r", "").replace("\n", " ")
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    cache_path.write_text("\n".join(lines), encoding="utf-8")
    return cache_path


def _load_srt_cache(cache_path: Path) -> Dict:
    content = cache_path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {"duration": 0.0, "segments": []}

    segments = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[0].split("-->", 1)]
        text = "\n".join(lines[1:]).strip()
        segments.append(
            {
                "start": _parse_srt_timestamp(start_raw),
                "end": _parse_srt_timestamp(end_raw),
                "text": text,
            }
        )

    duration = segments[-1]["end"] if segments else 0.0
    return {"duration": duration, "segments": segments}


def _register_nvidia_dll_paths() -> None:
    """Make pip-installed CUDA libraries findable on Windows.

    nvidia-cublas-cu12 / nvidia-cudnn-cu12 drop their DLLs in
    site-packages/nvidia/*/bin, which is not on the DLL search path. Without
    this, ctranslate2 fails with "Library cublas64_12.dll is not found" even
    though the package is installed.

    Both mechanisms are needed: add_dll_directory only affects loads that pass
    LOAD_LIBRARY_SEARCH_USER_DIRS, and ctranslate2 resolves cuBLAS with a plain
    LoadLibrary, which searches PATH instead. No-op off Windows, where the
    wheels set RPATH correctly.
    """
    import sys

    if sys.platform != "win32":
        return

    try:
        import nvidia  # type: ignore
    except ImportError:
        return

    bin_dirs = []
    for package_root in nvidia.__path__:
        root = Path(package_root)
        if root.is_dir():
            bin_dirs.extend(str(p) for p in root.glob("*/bin") if p.is_dir())

    if not bin_dirs:
        return

    if hasattr(os, "add_dll_directory"):
        for bin_dir in bin_dirs:
            try:
                os.add_dll_directory(bin_dir)
            except (OSError, FileNotFoundError):
                continue

    current = os.environ.get("PATH", "")
    missing = [d for d in bin_dirs if d not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + [current])


def _cuda_runtime_ready() -> tuple:
    """Check whether a CUDA run can actually succeed.

    ctranslate2 reporting a device is not enough: on Windows the cuBLAS/cuDNN
    DLLs are shipped separately from the driver, and their absence only surfaces
    deep inside transcribe() as "Library cublas64_12.dll is not found".
    Returns (ok, reason).
    """
    _register_nvidia_dll_paths()

    try:
        import ctranslate2  # type: ignore
    except ImportError:
        return False, "ctranslate2 is not installed"

    try:
        if ctranslate2.get_cuda_device_count() < 1:
            return False, "no CUDA device detected"
    except Exception as e:
        return False, f"CUDA probe failed: {e}"

    # The compute libraries load lazily, so probe them explicitly.
    import ctypes
    import sys

    if sys.platform == "win32":
        if not _can_load_library(ctypes, "cublas64_12.dll"):
            return False, "cublas64_12.dll is missing"

    return True, ""


def _can_load_library(ctypes_mod, name: str) -> bool:
    try:
        ctypes_mod.CDLL(name)
        return True
    except OSError:
        return False


CUDA_SETUP_HINT = (
    "To enable GPU transcription, install the CUDA runtime libraries into this "
    "environment:\n"
    "    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
    "(they ship the DLLs faster-whisper needs; the NVIDIA driver alone is not enough)"
)


def _resolve_device() -> str:
    """Pick the transcription device, honouring an explicit GUI/env choice.

    "auto" verifies the CUDA runtime rather than trusting a device count, so it
    never hands back a device that will fail once transcription starts.
    """
    requested = env("LOCAL_WHISPER_DEVICE", "auto").strip().lower()
    if requested != "auto":
        if requested == "cuda":
            # Explicit request still needs the DLL paths registered.
            _register_nvidia_dll_paths()
        return requested

    ok, reason = _cuda_runtime_ready()
    if ok:
        return "cuda"
    print(f"[transcribe/local] GPU unavailable ({reason}); using CPU", flush=True)
    return "cpu"


def transcribe_local(media_path: str, language: Optional[str] = None) -> Dict:
    """Run faster-whisper on a local file path, caching the result as .srt."""
    cache_path = _transcript_cache_path(media_path)
    if cache_path.exists():
        source_mtime = os.path.getmtime(media_path)
        cache_mtime = cache_path.stat().st_mtime
        if cache_mtime >= source_mtime:
            print(f"[transcribe/local] reusing cached transcript: {cache_path}", flush=True)
            cached = _load_srt_cache(cache_path)
            # Treat empty cache as invalid (likely from a failed/partial run) — delete and re-transcribe
            if not cached["segments"] or cached["duration"] <= 0.0:
                print(f"[transcribe/local] cache is empty/invalid, deleting: {cache_path}", flush=True)
                cache_path.unlink(missing_ok=True)
            else:
                print(
                    f"[transcribe/local] {len(cached['segments'])} cached segments, "
                    f"{cached['duration']:.0f}s of audio",
                    flush=True,
                )
                return cached

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    device = _resolve_device()
    whisper_model_name = env("LOCAL_WHISPER_MODEL", "base")
    print(f"[transcribe/local] faster-whisper model={whisper_model_name} device={device}", flush=True)

    from ..config import LOCAL_WHISPER_VAD_FILTER, LOCAL_WHISPER_VAD_PARAMETERS

    transcribe_kwargs = {
        "audio": media_path,
        "language": language,
        "beam_size": 5,
        "condition_on_previous_text": False,
    }
    if LOCAL_WHISPER_VAD_FILTER:
        transcribe_kwargs["vad_filter"] = True
        transcribe_kwargs["vad_parameters"] = LOCAL_WHISPER_VAD_PARAMETERS
    else:
        transcribe_kwargs["vad_filter"] = False

    def _run(dev: str):
        """Load the model and drain the segment generator on `dev`.

        The generator must be consumed here, not returned lazily: ctranslate2
        loads cuBLAS on first use, so a broken CUDA install raises inside
        iteration rather than at construction. Draining it here is what lets the
        caller catch the failure and retry on CPU.
        """
        compute = "float16" if dev == "cuda" else "int8"
        model = WhisperModel(whisper_model_name, device=dev, compute_type=compute)
        segments_iter, info = model.transcribe(**transcribe_kwargs)
        drained = [
            {
                "start": float(s.start),
                "end": float(s.end),
                "text": (s.text or "").strip(),
            }
            for s in segments_iter
        ]
        return drained, info

    try:
        segments, info = _run(device)
    except Exception as e:
        if device != "cuda":
            raise
        print(f"[transcribe/local] GPU transcription failed ({e})", flush=True)
        print(f"[transcribe/local] {CUDA_SETUP_HINT}", flush=True)
        print("[transcribe/local] retrying on CPU", flush=True)
        device = "cpu"
        segments, info = _run(device)

    duration = float(getattr(info, "duration", 0.0)) or (segments[-1]["end"] if segments else 0.0)
    print(f"[transcribe/local] {len(segments)} segments, {duration:.0f}s of audio", flush=True)
    transcript = {"duration": duration, "segments": segments}
    cache_path = _write_srt_cache(media_path, transcript)
    print(f"[transcribe/local] wrote cache: {cache_path}", flush=True)
    return transcript
