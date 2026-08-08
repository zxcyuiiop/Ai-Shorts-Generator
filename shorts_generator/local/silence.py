"""Silence (pause) detection and jump-cut removal, fully standalone.

Uses ffmpeg's ``silencedetect`` filter to find pauses in the audio track of a
clip, inverts them into speech segments, and re-encodes with trim/concat in a
single ffmpeg pass. Standalone on purpose: it does NOT use clipper's
GPU-encoder helpers (_get_video_encoder / _video_encoder_args) — it always
encodes libx264 crf 18 preset veryfast + aac 128k.

INTENDED CALL POINT (wired by a later integration step, NOT here):
  In shorts_generator/local/clipper.py, ``crop_clip_local`` — BETWEEN
  ``_cut_subclip`` (produces cut_path) and ``_reframe_vertical`` (consumes
  cut_path). Suggested flow::

      if env("SILENCE_CUT", "1") not in ("0", "false", "no"):
          silences = detect_silences(
              cut_path,
              noise_db=float(env("SILENCE_NOISE_DB", "-35")),
              min_silence=float(env("SILENCE_MIN_DUR", "0.45")),
          )
          segs = build_keep_segments(
              get_duration(cut_path), silences,
              keep_extra=float(env("SILENCE_KEEP_EXTRA", "0.15")),
          )
          if segs is not None and sum(e - s for s, e in segs) >= 2.0:
              tight_path = cut_path + ".tight.mp4"
              cut_pauses(cut_path, tight_path, segs)
              os.replace(tight_path, cut_path)   # reframe then reads the cut file
          # else: keep the original cut (fewer than 2.0s kept -> not worth it)

  Gate env vars (read via ..config.env so GUI overrides work):
    SILENCE_CUT        ('1')      master switch; 0/false/no disables
    SILENCE_NOISE_DB   ('-35')    silencedetect noise threshold in dB
    SILENCE_MIN_DUR    ('0.45')   minimum pause length in seconds
    SILENCE_KEEP_EXTRA ('0.15')   seconds of silence kept on each side of speech
  Min clipped length guard: if total kept time < 2.0s, skip cutting entirely.

Console is cp1252 on Windows: keep every print below ASCII-only.
"""
import os
import re
import subprocess
from typing import List, Optional, Tuple

DEFAULT_NOISE_DB = -35.0
DEFAULT_MIN_SILENCE = 0.45
DEFAULT_KEEP_EXTRA = 0.15
MIN_SEGMENT_SEC = 0.1          # drop speech segments shorter than this
NO_CUT_COVER_RATIO = 0.95      # >=95% of the clip kept -> nothing worth cutting
MIN_KEPT_TOTAL_SEC = 2.0       # integration guard: below this, skip cutting
FFMPEG_TIMEOUT = 180           # matches clipper._run_ffmpeg


def _run(cmd: list, what: str, timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe command, raising RuntimeError with stderr tail."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg ({what}) timed out after {timeout}s")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        tail = "\n".join(detail.splitlines()[-6:]) or f"exit status {proc.returncode}"
        raise RuntimeError(f"ffmpeg failed during {what}:\n{tail}")
    return proc


def get_duration(path: str) -> float:
    """Media duration in seconds via ffprobe. Raises if it cannot be read."""
    proc = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        "probing duration",
        timeout=30,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe returned no duration for {path!r}: {proc.stdout.strip()!r}")


def _has_audio_stream(path: str) -> bool:
    """True when ffprobe reports at least one audio stream."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode == 0 and proc.stdout.strip() != ""


def _merge_ranges(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Sort by start and merge overlapping/touching ranges."""
    out: List[Tuple[float, float]] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def detect_silences(media_path: str,
                    noise_db: float = DEFAULT_NOISE_DB,
                    min_silence: float = DEFAULT_MIN_SILENCE,
                    log=print) -> List[Tuple[float, float]]:
    """Find silent ranges (start, end) in seconds via ffmpeg silencedetect.

    ffmpeg reports 'silence_start: X' and 'silence_end: X | silence_duration: Y'
    on stderr. A trailing silence_start without a matching silence_end is closed
    at the media duration (ffprobe). Overlapping reports are merged.
    """
    proc = _run(
        ["ffmpeg", "-hide_banner", "-nostats",
         "-i", media_path,
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        "detecting silence",
    )
    silences: List[Tuple[float, float]] = []
    open_start: Optional[float] = None
    for line in (proc.stderr or "").splitlines():
        m = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
        if m:
            open_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*(-?\d+(?:\.\d+)?)", line)
        if m and open_start is not None:
            end = float(m.group(1))
            if end > open_start:
                silences.append((open_start, end))
            open_start = None
    if open_start is not None:
        # Silence runs to the end of the file: close it at the media duration.
        try:
            end = get_duration(media_path)
        except RuntimeError:
            end = open_start
        if end > open_start:
            silences.append((open_start, end))
    silences = _merge_ranges(silences)
    log(f"[clip/local] silence: found {len(silences)} pauses")
    return silences


def build_keep_segments(duration: float,
                        silences: list,
                        keep_extra: float = DEFAULT_KEEP_EXTRA) -> Optional[List[Tuple[float, float]]]:
    """Invert silence ranges into speech segments to keep.

    Each speech segment keeps ``keep_extra`` seconds of the surrounding silence
    on both sides, so a pause shorter than 2*keep_extra effectively survives the
    cut. Segments are clamped to [0, duration]; anything under 0.1s is dropped.
    Returns None when nothing is worth cutting: no usable segments, or the kept
    ranges still cover >=95% of the clip.
    """
    if duration <= 0:
        return None
    segments: List[Tuple[float, float]] = []
    cursor = 0.0
    for s_start, s_end in _merge_ranges(
        [(max(0.0, s), min(float(duration), e)) for s, e in silences if e > s]
    ):
        seg = (max(0.0, cursor - keep_extra), min(float(duration), s_start + keep_extra))
        if seg[1] - seg[0] >= MIN_SEGMENT_SEC:
            segments.append(seg)
        cursor = max(cursor, s_end)
    seg = (max(0.0, cursor - keep_extra), float(duration))
    if seg[1] - seg[0] >= MIN_SEGMENT_SEC:
        segments.append(seg)
    segments = _merge_ranges(segments)
    if not segments:
        return None
    kept = sum(e - s for s, e in segments)
    if kept >= NO_CUT_COVER_RATIO * duration:
        return None
    return segments


def cut_pauses(in_path: str, out_path: str, segments: list, log=print) -> str:
    """Re-encode keeping only the given (start, end) segments, in one ffmpeg pass.

    Per segment i: trim/setpts for video (and atrim/asetpts for audio when the
    media has an audio stream), then concat n=N:v=1:a=1 (or v-only without an
    audio stream). Encoded standalone with libx264 crf 18 preset veryfast and
    aac 128k — the project GPU encoder helpers are deliberately NOT used here.
    """
    segments = [(float(s), float(e)) for s, e in segments if e > s]
    if not segments:
        raise ValueError("cut_pauses: no segments to keep")
    duration = get_duration(in_path)
    has_audio = _has_audio_stream(in_path)

    parts: List[str] = []
    concat_inputs: List[str] = []
    for i, (start, end) in enumerate(segments):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
            concat_inputs.append(f"[v{i}][a{i}]")
        else:
            concat_inputs.append(f"[v{i}]")
    n = len(segments)
    if has_audio:
        parts.append(
            f"{''.join(concat_inputs)}concat=n={n}:v=1:a=1[vout][aout]"
        )
        maps = ["-map", "[vout]", "-map", "[aout]"]
    else:
        parts.append(f"{''.join(concat_inputs)}concat=n={n}:v=1:a=0[vout]")
        maps = ["-map", "[vout]"]

    cmd = (
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", in_path,
         "-filter_complex", ";".join(parts)]
        + maps
        + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    )
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", out_path]
    _run(cmd, "cutting silent pauses")

    kept = sum(e - s for s, e in segments)
    log(f"[clip/local] silence: kept {kept:.1f}s of {duration:.1f}s")
    return out_path
