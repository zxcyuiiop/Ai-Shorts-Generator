"""Blur-padding to 9:16 vertical: full frame over a dimmed blurred background.

Classic blurred-background fit -- the ORIGINAL composition is never cropped:

  * SOURCE: use the ORIGINAL landscape clip (e.g. 1920x1080), NOT the
    already-cropped 9:16 reframe (602x1072). Blurring a 9:16 crop would
    force the foreground to re-scale to 1080x1920 and fill the whole
    canvas, leaving zero room for the bars -- the bug where the blur
    silently became a plain re-encode. The landscape source is found via
    ``source_path`` when the caller knows it, otherwise rediscovered by
    scanning the job folder for the matching ``.cut.mp4`` / source file;
  * background: the source scaled to cover OUT_W x OUT_H (1080x1920),
    centre-cropped to the canvas, dimmed (BLURPAD_DIM, default 0.5) and
    blurred (gblur sigma=BLURPAD_SIGMA, default 22) -- the dim drops the
    backdrop to ~0.5x of the foreground brightness (matching the TikTok
    reference frames, where the bars sit clearly darker than the content
    on both bright screen-recordings and dark cinematic material) while
    the blur keeps it soft enough to never fight the sharp foreground;
  * foreground: the WHOLE source frame, scale width-to-canvas
    (1920x1080 -> 1080x608), rounded down to even numbers (yuv420p
    rejects odd dims), optionally pre-shrunk by BLURPAD_FG_SCALE percent
    (default 100, clamped 50..100);
  * overlaid dead-centre: overlay=(W-w)/2:(H-h)/2, so symmetric
    letterbox bars appear above and below the frame (~656 px each for
    16:9), exactly the "slightly darkened, heavily blurred copy of the
    frame fills the free space" TikTok look.

Intended call point: inside finalize_clip_local (local/clipper.py), which
passes the draft (already silence-cut / reframed) plus enough hints to
locate the landscape source. This module must NOT import local/clipper.py
(clipper imports this module -- circular import otherwise), so it carries
its own minimal ffmpeg/ffprobe runners.
"""
import glob
import os
import re
import shutil
import subprocess

from ..config import env

OUT_W = 1080              # output width
OUT_H = 1920              # output height


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _fg_scale_percent() -> float:
    """BLURPAD_FG_SCALE env: foreground pre-shrink, percent of the full box.

    Read at use time (per-request overrides); clamp 50..100 so a fat-fingered
    value can never produce a postage stamp or an overflow. Invalid -> 100.
    """
    try:
        return _clamp(float(str(env("BLURPAD_FG_SCALE", "100") or "100").strip()), 50.0, 100.0)
    except (TypeError, ValueError):
        return 100.0


def _blur_sigma() -> float:
    """BLURPAD_SIGMA env: gblur sigma for the background layer. Invalid -> 22."""
    try:
        return float(str(env("BLURPAD_SIGMA", "22") or "22").strip())
    except (TypeError, ValueError):
        return 22.0


def _dim_amount() -> float:
    """BLURPAD_DIM env: background darkening 0..0.7 (eq brightness=-X before blur).

    Default 0.5 -- on a bright screen-recording (fg luminance ~230) this drops
    the backdrop to ~115, which finally reads as "noticeably dimmed" to the
    eye; on dark cinematic content (fg ~35) it lands ~17-25, matching the
    reference TikTok blur-bar frames where the bars sit just under the
    foreground brightness. The earlier 0.32 kept the bars too close to the
    fg on bright sources, so the dim was optically invisible there.
    Clamped at 0.7: beyond that the backdrop is essentially black and the
    blur stops being visible.
    """
    try:
        return _clamp(float(str(env("BLURPAD_DIM", "0.5") or "0.5").strip()), 0.0, 0.7)
    except (TypeError, ValueError):
        return 0.32


def blurpad_enabled_for(aspect_ratio: str) -> bool:
    """True when blur bars are wanted for this aspect ratio.

    Env BLUR_BARS (default '1') is the master switch; any value other than
    0/false/no (case-insensitive) counts as enabled. Only ever applies to
    '9:16' -- other ratios keep the plain crop.
    """
    raw = str(env("BLUR_BARS", "1") or "").strip().lower()
    enabled = raw not in ("0", "false", "no")
    return enabled and aspect_ratio == "9:16"


def _has_audio(path: str) -> bool:
    """ffprobe check: does the input carry at least one audio stream?"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _probe_video_size(path: str):
    """(width, height) of the first video stream, or None when unprobeable."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        if proc.returncode != 0:
            return None
        m = re.search(r"(\d+)\s*,\s*(\d+)", proc.stdout or "")
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _is_landscape(size) -> bool:
    """True when a (w, h) probe result is landscape (strictly wider than tall).

    Square (1:1) is treated as NON-landscape: it carries no usable bars
    material either, so a square source still falls back to blurring the
    draft (where fit-by-height at least keeps some frame visible).
    """
    return bool(size) and size[0] > size[1]


def _find_landscape_source(draft_path: str, log=print):
    """Locate the original landscape clip a 9:16 draft was cut from.

    Search order, most precise first:

      1. ``<same-dir>/<draft-stem>.cut.mp4`` — the step-1 cut kept next to the
         draft while the pipeline runs (crop_clip_local removes it in a
         finally; when finalize is deferred the draft is approved later, so
         the cut is usually gone — still worth one cheap stat);
      2. ``<output-dir>/source_<video-id>.<ext>`` — the full downloaded
         source. The video id is the leading ``[A-Za-z0-9_-]{6,}`` chunk of
         the draft stem (drafts are named ``<id>_01.mp4``), and the output
         dir is the job folder's parent (``output/<id>/<id>_01.mp4``).

    A candidate wins only when ffprobe reports a landscape frame; anything
    else returns None and the caller falls back to blurring the draft.
    """
    try:
        draft_dir = os.path.dirname(os.path.abspath(draft_path))
        stem = os.path.splitext(os.path.basename(draft_path))[0]

        candidates = [os.path.join(draft_dir, stem + ".cut.mp4")]

        m = re.match(r"([A-Za-z0-9_-]+?)(?:_\d+)?$", stem)
        if m:
            video_id = m.group(1)
            output_dir = os.path.dirname(draft_dir)
            for ext in (".mp4", ".mkv", ".webm", ".mov"):
                candidates.append(os.path.join(output_dir, f"source_{video_id}{ext}"))
            # Last resort: any source_<id>.* whose probe says landscape.
            for path in sorted(glob.glob(os.path.join(output_dir, f"source_{video_id}.*"))):
                if path not in candidates:
                    candidates.append(path)

        seen = set()
        for path in candidates:
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            size = _probe_video_size(path)
            if _is_landscape(size):
                log(f"[clip/local] blurpad: landscape source found: {path} ({size[0]}x{size[1]})")
                return path
        log("[clip/local] blurpad: no landscape source found — falling back to the draft itself")
        return None
    except Exception as e:
        log(f"[clip/local] blurpad: source lookup failed ({e}) — using the draft itself")
        return None


def _clip_to_source_time(t_clip: float, segs) -> float:
    """Map a timestamp inside the silence-cut draft back onto the source.

    The draft is a tight concat of kept [start, end) windows; walking those
    windows in order while subtracting their lengths from ``t_clip`` lands
    on the source timestamp of the same frame. Out-of-range input clamps to
    the last kept window's end — a clamp is always closer than nothing.
    """
    if not segs:
        return t_clip
    remaining = max(0.0, float(t_clip))
    last_end = None
    for start, end in segs:
        last_end = end
        seg_len = max(0.0, end - start)
        if remaining < seg_len:
            return start + remaining
        remaining -= seg_len
    return float(last_end) if last_end is not None else max(0.0, float(t_clip))


def _run_ffmpeg(cmd: list, log) -> None:
    """Run ffmpeg, raising with its stderr tail on failure.

    Local runner on purpose: importing clipper's one would create a circular
    import once clipper wires blurpad in.
    """
    proc = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=300,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        raise RuntimeError(f"blurpad ffmpeg failed (exit {proc.returncode}): {tail}")


def _render_blurpad(fg_path: str, fg_seek_in, bg_path: str, bg_seek_in,
                    out_path: str, audio_from: str, log=print) -> str:
    """Single ffmpeg pass: blurred dimmed ``bg_path`` canvas + centred ``fg_path``.

    Foreground: scaled width-to-canvas (scale=OUT_W:-2 — a 16:9 landscape
    source becomes 1080x608 and leaves ~656 px bars above/below; a 9:16
    fallback input becomes the full 1080x1920 and simply degenerates to a
    plain re-encode instead of a crash), then shrunk by BLURPAD_FG_SCALE
    percent. Background: cover-scale + centre-crop + eq dim + gblur.
    ``fg_seek_in`` / ``bg_seek_in`` are optional input -ss offsets (seconds)
    applied to their respective inputs — used to align the source-fed
    background with a draft-fed foreground. Audio is stream-copied from
    ``audio_from`` when it has an audio stream.
    """
    scale_pct = _fg_scale_percent()
    sigma = _blur_sigma()
    dim = _dim_amount()
    same_input = os.path.abspath(fg_path) == os.path.abspath(bg_path)
    log(f"[clip/local] blurpad: fg={fg_path} bg={bg_path} -> {out_path} "
        f"({OUT_W}x{OUT_H}, fg fit-width {scale_pct:g}%, blur {sigma:g}, dim {dim:g})")

    if same_input and not bg_seek_in:
        # One input, split into bg/fg branches.
        filter_complex = (
            f"[0:v]split[a][b];"
            f"[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},eq=brightness=-{dim:g},gblur=sigma={sigma:g}[bg];"
            f"[b]scale={OUT_W}:-2,"
            f"scale=trunc(iw*{scale_pct:g}/100/2)*2:trunc(ih*{scale_pct:g}/100/2)*2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if fg_seek_in:
            cmd += ["-ss", f"{fg_seek_in:.3f}"]
        cmd += ["-i", fg_path, "-filter_complex", filter_complex, "-map", "[v]"]
    else:
        # Two inputs: 1:v is the (possibly seek-offset) background source.
        filter_complex = (
            f"[1:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},eq=brightness=-{dim:g},gblur=sigma={sigma:g}[bg];"
            f"[0:v]scale={OUT_W}:-2,"
            f"scale=trunc(iw*{scale_pct:g}/100/2)*2:trunc(ih*{scale_pct:g}/100/2)*2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if fg_seek_in:
            cmd += ["-ss", f"{fg_seek_in:.3f}"]
        cmd += ["-i", fg_path]
        if bg_seek_in:
            cmd += ["-ss", f"{bg_seek_in:.3f}"]
        cmd += ["-i", bg_path, "-filter_complex", filter_complex, "-map", "[v]"]

    if audio_from and _has_audio(audio_from):
        idx = 0 if os.path.abspath(audio_from) == os.path.abspath(fg_path) else 1
        cmd += ["-map", f"{idx}:a:0?", "-c:a", "copy"]
        log(f"[clip/local] blurpad: audio stream found in input {idx}, copying")
    else:
        log("[clip/local] blurpad: no audio stream, video only")
    cmd += [
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-shortest",
        out_path,
    ]
    _run_ffmpeg(cmd, log)
    try:
        size = os.path.getsize(out_path)
        log(f"[clip/local] blurpad: done, size={size} bytes -> {out_path}")
    except OSError:
        pass
    return out_path


def apply_blur_padding(in_path: str, out_path: str, log=print,
                       source_path: str = None) -> str:
    """Render a clip over a dimmed blurred backdrop of the ORIGINAL frame.

    ``in_path`` is the draft/final clip (usually the 9:16 reframe — itself
    perfectly fine as the foreground + audio source, since it carries the
    silence cut). ``source_path`` (optional) is the original landscape clip
    the draft was cut from; when omitted it is rediscovered by
    ``_find_landscape_source``. When no landscape source can be found the
    draft itself feeds the backdrop too — the effect then degenerates to a
    full-canvas blur behind a full-canvas frame (invisible but harmless),
    which beats dropping the clip.

    IMPORTANT: without a landscape source there is nothing to letterbox —
    do not "fix" that by re-scaling the 9:16 draft (that was the bug where
    the fg silently re-filled the whole canvas). Returns out_path.
    """
    bg_path = source_path
    if not (bg_path and os.path.isfile(bg_path) and _is_landscape(_probe_video_size(bg_path))):
        bg_path = _find_landscape_source(in_path, log=log)
    if bg_path and os.path.isfile(bg_path):
        return _render_blurpad(in_path, None, bg_path, None, out_path,
                               audio_from=in_path, log=log)
    return _render_blurpad(in_path, None, in_path, None, out_path,
                           audio_from=in_path, log=log)


def apply_blur_padding_for_ar(in_path: str, out_path: str, aspect_ratio: str,
                              log=print, source_path: str = None) -> str:
    """Aspect-aware wrapper used by the finalize stage: 9:16 gets the blur pass,
    any other ratio is copied through unchanged.

    Copied instead of re-rendered on purpose: a 1:1 draft already has the
    right geometry, and a plain copy is lossless and instant.
    """
    if aspect_ratio == "9:16":
        return apply_blur_padding(in_path, out_path, log=log, source_path=source_path)
    log(f"[clip/local] blurpad: aspect {aspect_ratio} != 9:16, passthrough copy")
    shutil.copy2(in_path, out_path)
    return out_path
