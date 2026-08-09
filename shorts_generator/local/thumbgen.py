"""Cover-thumbnail extraction for rendered shorts, self-contained via ffmpeg.

Design notes
------------
- One frame is picked at ``duration * at_percent`` so the caller never has to
  guess a timestamp: ffprobe gives the duration, then ffmpeg seeks to the
  offset with ``-ss`` BEFORE ``-i`` (fast input seek; for a single frame the
  keyframe-snapping error is acceptable) and grabs exactly one frame
  (``-frames:v 1``) scaled to at most 1080px wide (``scale=min(1080,iw):-2``
  keeps the original aspect with an even height) at a visually lossless JPEG
  quality (``-q:v 3``).
- ``at_percent`` comes from the explicit argument, env ``THUMB_AT_PERCENT``
  (default: 12 — the intro is usually past, the punchline usually not yet),
  and is clamped to 1..90 so the seek never lands on the black first/last
  frame.
- An optional title overlay (arg ``title`` or env ``THUMB_TITLE``) is drawn
  with the drawtext filter: wrapped to ~22 chars/line, at most 3 lines,
  horizontally centered at ~28% of the height, white text on a thick black
  border. drawtext + fontconfig is the most fragile part of ffmpeg (Windows
  especially), so ANY drawtext failure falls back to the plain frame — a
  thumbnail without a title is better than no thumbnail.
- The target name never overwrites: ``<stem>_thumb.jpg`` (next to the video
  unless ``out_path`` is given), then ``<stem>_thumb_2.jpg`` and so on.
- Same house style as music.py: module runs ffmpeg/ffprobe itself, errors
  raise RuntimeError with a stderr-tail flavour.
"""
import os
import shutil
import subprocess

from ..config import env

# Module-level attribute so tests can stub `thumbgen.subprocess.run`;
# every call site MUST go through `_subprocess.run` (direct `subprocess.run`
# is module-attribute-patched by the real subprocess module, tests can't see it).
_subprocess = subprocess

# Same house style as clipper._run_ffmpeg: error-only output, bounded runtime.
FFMPEG_TIMEOUT = 60  # seconds — a single-frame seek can never take long

DEFAULT_AT_PERCENT = 12.0
MIN_AT_PERCENT = 1.0
MAX_AT_PERCENT = 90.0

_MAX_TITLE_LINE = 22    # chars per drawtext line
_MAX_TITLE_LINES = 3
_TITLE_Y_FRACTION = 0.28  # ~28% from the top (subject's face usually sits lower)


def _clamp_percent(value) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = DEFAULT_AT_PERCENT
    return max(MIN_AT_PERCENT, min(MAX_AT_PERCENT, pct))


def _probe_duration(video_path):
    """Duration in seconds via ffprobe, or None (never raises)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = _subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        duration = float((proc.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _wrap_title(title: str) -> str:
    """Wrap to <=3 lines of <=~22 chars, word-wise; the overflow is dropped.

    The word that does not fit is carried to the next line (`current = word`),
    not dropped — losing a word mid-title can quietly break its meaning.
    """
    words = " ".join(str(title).split()).split()
    lines, current = [], ""
    for word in words:
        cand = f"{current} {word}".strip()
        if len(cand) <= _MAX_TITLE_LINE:
            current = cand
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= _MAX_TITLE_LINES:
                break
    if current and len(lines) < _MAX_TITLE_LINES:
        lines.append(current)
    return "\n".join(lines[:_MAX_TITLE_LINES])


def _drawtext_escape(text: str) -> str:
    """Escape characters that are special inside a drawtext filter string.

    Backslash first, then the filter-level separators. Apostrophes are simply
    dropped — argv is passed shell-less, and inside drawtext's own quoting a
    literal ' cannot be protected reliably across platforms.
    """
    return (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "")
            .replace("%", "\\%")
            .replace(",", "\\,")
            .replace("\n", "\\n")
    )


def _next_free_path(video_path: str, out_path=None) -> str:
    """First non-existing <stem>_thumb[_N].jpg — never overwrite anything."""
    if out_path:
        directory = os.path.dirname(os.path.abspath(out_path))
        stem = os.path.splitext(os.path.basename(out_path))[0]
    else:
        directory = os.path.dirname(os.path.abspath(video_path))
        stem = os.path.splitext(os.path.basename(video_path))[0] + "_thumb"
    candidate = os.path.join(directory, f"{stem}.jpg")
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = os.path.join(directory, f"{stem}_{n}.jpg")
    return candidate


def _drawtext_filter(title: str, font=None) -> str:
    """drawtext string for the wrapped, escaped title; `font` may be None."""
    font_opt = f"font='{font}':" if font else ""
    expr = _drawtext_escape(_wrap_title(title))
    return (f"drawtext={font_opt}text='{expr}':fontcolor=white:borderw=3:"
            f"bordercolor=black:fontsize=h/16:"
            f"x=(w-text_w)/2:y=h*{_TITLE_Y_FRACTION:.2f}")


def _build_cmd(ffmpeg, video_path, out_path, offset, vf=None) -> list:
    """Assemble the ffmpeg argv. `-ss` goes before `-i` (fast input seek)."""
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-ss", f"{offset:.3f}", "-i", video_path, "-frames:v", "1"]
    scale = "scale='min(1080,iw)':-2"
    cmd += ["-vf", f"{scale},{vf}" if vf else scale]
    cmd += ["-q:v", "3", out_path]
    return cmd


def _run_extract(cmd, out_path) -> bool:
    """Run ffmpeg once; True only when it exited 0 AND produced the file."""
    try:
        proc = _subprocess.run(cmd, capture_output=True, text=True,
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg thumbnail extraction timed out after {FFMPEG_TIMEOUT}s")
    except OSError as e:
        raise RuntimeError(f"ffmpeg could not be started: {e}")
    return proc.returncode == 0 and os.path.isfile(out_path)


def _resolve_title(title):
    """Empty/None -> None; False disables explicitly; None alone falls through
    to env THUMB_TITLE. Kept separate so the fallback chain is testable."""
    if title is None:
        return (env("THUMB_TITLE", "") or "").strip() or None
    if title is False:
        return None
    return str(title).strip() or None


def make_thumbnail(video_path: str, out_path=None, title=None,
                   at_percent=None) -> str:
    """Extract a cover JPEG for `video_path`. Returns the written path.

    - `out_path`: explicit target; a sibling `_N` name is picked instead when
      it already exists. Default: ``<video-stem>_thumb.jpg`` next to the clip.
    - `title`: overlay text; None falls back to env THUMB_TITLE, empty string
      / False disables the overlay explicitly.
    - `at_percent`: seek position as a percentage of the duration; read from
      env THUMB_AT_PERCENT when not given, clamped to 1..90.

    Raises RuntimeError when ffmpeg/ffprobe are missing or extraction failed.
    """
    title = _resolve_title(title)

    if at_percent is None:
        at_percent = env("THUMB_AT_PERCENT", str(DEFAULT_AT_PERCENT))
    pct = _clamp_percent(at_percent)

    duration = _probe_duration(video_path) or 0.0
    offset = duration * pct / 100.0

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    target = _next_free_path(video_path, out_path)

    if title:
        # Fontconfig lookup is the failure point on bare Windows installs, so
        # try named fonts, then drawtext's default font, then no title at all.
        for font in ("DejaVu Sans", "Arial", None):
            if _run_extract(
                    _build_cmd(ffmpeg, video_path, target, offset,
                               _drawtext_filter(title, font)), target):
                return target

    if _run_extract(_build_cmd(ffmpeg, video_path, target, offset), target):
        return target
    raise RuntimeError(f"ffmpeg thumbnail extraction failed for {video_path}")
