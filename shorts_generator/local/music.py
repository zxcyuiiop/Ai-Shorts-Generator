"""Background music mixing for rendered shorts, all local via ffmpeg.

The pipeline calls this after a clip is rendered and cropped: the music bed is
looped to the clip length, ducked to `volume`, mixed with the clip's own audio
(kept when present), and the clip file is replaced in place so downstream code
never has to know it happened.

Configuration is env-driven -- see music_settings_from_env().
"""
import os
import subprocess
import time

from ..config import env

MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# Project root: .../shorts_generator/local/music.py -> up three levels.
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Same house style as clipper._run_ffmpeg: error-only output, bounded runtime.
FFMPEG_TIMEOUT = 180  # seconds


def music_file_valid(path) -> bool:
    """True when `path` is an existing file with a supported audio extension."""
    if not path:
        return False
    ext = os.path.splitext(str(path))[1].lower()
    return ext in MUSIC_EXTENSIONS and os.path.isfile(path)


def music_settings_from_env() -> dict:
    """Read the music bed settings in one place.

    Returns:
        enabled: True when MUSIC_ENABLED is truthy ("1", "true", "yes", "on";
            default "0").
        file: the music path from MUSIC_FILE, or None. Relative paths are
            resolved against the project root; the value is returned only when
            it points at a valid music file (music_file_valid).
        volume: MUSIC_VOLUME is a percent 0..100 mapped to the ffmpeg 0..2
            scale via v/50 (100% = 2.0, the default 40% = 0.8). Unparseable
            values fall back to 40.
    """
    enabled = env("MUSIC_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")

    raw_path = (env("MUSIC_FILE", "") or "").strip()
    music_path = None
    if raw_path:
        if not os.path.isabs(raw_path):
            raw_path = os.path.join(PROJECT_ROOT, raw_path)
        raw_path = os.path.abspath(raw_path)
        if music_file_valid(raw_path):
            music_path = raw_path

    raw_volume = (env("MUSIC_VOLUME", "40") or "").strip()
    try:
        percent = float(raw_volume)
    except (TypeError, ValueError):
        percent = 40.0
    percent = max(0.0, min(100.0, percent))

    return {"enabled": enabled, "file": music_path, "volume": percent / 50.0}


def _has_audio_stream(path: str) -> bool:
    """True when ffprobe finds at least one audio stream. False (not an
    exception) when ffprobe is missing or the probe itself fails -- the mixer
    then takes the music-only path, which is the safe fallback."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "audio" in (proc.stdout or "").lower()


def mix_music(clip_path: str, music_path: str, volume: float, log=print) -> str:
    """Mix a looping music bed under `clip_path` in place. Returns clip_path.

    The mix renders to a temp file next to the clip and then os.replace()s it
    over the original, so a failed or interrupted run leaves the original
    untouched.

    - volume is clamped to the ffmpeg 0..2 range.
    - When the clip has no audio stream of its own, the music alone becomes
      the soundtrack (no amix needed).
    - The music loops (-stream_loop -1) and the mix ends with the clip
      (amix duration=first), so any music length works.
    - Video streams through (-c:v copy); only audio is re-encoded (aac 128k).

    Raises RuntimeError with the ffmpeg stderr tail on failure.
    """
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        volume = 0.8
    volume = max(0.0, min(2.0, volume))

    clip_path = os.path.abspath(clip_path)
    music_path = os.path.abspath(music_path)
    log(f"[clip/local] music: mixing {os.path.basename(music_path)} "
        f"into {os.path.basename(clip_path)} at volume {volume:.2f}")

    has_audio = _has_audio_stream(clip_path)
    music_input = ["-stream_loop", "-1", "-i", music_path]
    if has_audio:
        filtergraph = (
            f"[1:a]volume={volume}[m];"
            f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    else:
        log(f"[clip/local] music: no audio stream in "
            f"{os.path.basename(clip_path)}, using music as soundtrack")
        filtergraph = f"[1:a]volume={volume}[a]"

    tmp_path = os.path.join(
        os.path.dirname(clip_path), f".music_tmp_{os.getpid()}_{int(time.time() * 1000)}.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", clip_path, *music_input,
           "-filter_complex", filtergraph,
           "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
           "-shortest", tmp_path]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        _remove_quietly(tmp_path)
        raise RuntimeError(
            f"ffmpeg music mix timed out after {FFMPEG_TIMEOUT}s for {clip_path}")
    except OSError as e:
        _remove_quietly(tmp_path)
        raise RuntimeError(f"ffmpeg could not be started for music mix: {e}")

    if proc.returncode != 0:
        _remove_quietly(tmp_path)
        tail = (proc.stderr or "").strip()[-400:]
        raise RuntimeError(f"ffmpeg music mix failed for {clip_path}: {tail}")

    os.replace(tmp_path, clip_path)
    log(f"[clip/local] music: done {os.path.basename(clip_path)}")
    return clip_path


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
