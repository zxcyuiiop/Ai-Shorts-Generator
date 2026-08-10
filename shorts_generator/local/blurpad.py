"""Blur-padding to 9:16 vertical: full frame over a dimmed blurred background.

Classic blurred-background fit -- NOTHING is cropped:

  * background: the video scaled to cover OUT_W x OUT_H (1080x1920),
    centre-cropped to the canvas size, dimmed (BLURPAD_DIM, default 0.32)
    and blurred (gblur sigma=BLURPAD_SIGMA, default 22) -- the default dim
    drops the backdrop to roughly 0.7x of the foreground brightness
    (matching the TikTok reference frames, where the bars sit clearly
    darker than the content) while the blur keeps it soft enough
    to never fight the sharp foreground;
  * foreground: the WHOLE frame, scaled to fit inside the fg box with
    force_original_aspect_ratio=decrease (16:9 source -> 1080x608), width/height
    rounded down to even numbers (yuv420p rejects odd dims), optionally
    pre-shrunk by BLURPAD_FG_SCALE percent (default 100, clamped 50..100);
  * overlaid dead-centre: overlay=(W-w)/2:(H-h)/2, so the blur fills whatever
    the foreground leaves (pillar/letterbox bars for any source aspect).

Intended call point: inside crop_clip_local in shorts_generator/local/clipper.py,
RIGHT AFTER `_reframe_vertical(cut_path, out_path, aspect_ratio)`:

    if blurpad_enabled_for(aspect_ratio):
        swap_path = out_path + ".prerender.mp4"
        os.replace(out_path, swap_path)   # reframe output becomes blurpad input
        apply_blur_padding(swap_path, out_path)
        os.remove(swap_path)              # best-effort cleanup

i.e. reframed 9:16 output -> blurpad in-place-ish replace -> pipeline continues
with the TikTok overlay on the 1080x1920 result. This module must NOT import
local/clipper.py (clipper will import this module -- circular import otherwise),
so it carries its own minimal ffmpeg runner.
"""
import os
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

    Default 0.32 -- the backdrop lands around 0.7x of the foreground's
    brightness on typical content, mirroring the TikTok blur-bar reference
    where the bars sit clearly darker than the centre content. Clamped at
    0.7: beyond that the backdrop is essentially black and the blur stops
    being visible.
    """
    try:
        return _clamp(float(str(env("BLURPAD_DIM", "0.32") or "0.32").strip()), 0.0, 0.7)
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


def apply_blur_padding(in_path: str, out_path: str, log=print) -> str:
    """Render in_path over a dimmed blurred copy of itself, 1080x1920, one pass.

    Foreground: whole frame scaled to FIT the OUT_W x OUT_H box
    (force_original_aspect_ratio=decrease -- no cropping anywhere), then
    shrunk by BLURPAD_FG_SCALE percent; overlay is always centred, so any
    source aspect gets symmetric blurred bars. Background: cover-scale +
    centre-crop + eq brightness dim + gblur. Audio is stream-copied when
    present, omitted otherwise. Returns out_path.
    """
    scale_pct = _fg_scale_percent()
    sigma = _blur_sigma()
    dim = _dim_amount()
    log(f"[clip/local] blurpad: {in_path} -> {out_path} "
        f"({OUT_W}x{OUT_H}, fg fit {scale_pct:g}%, blur {sigma:g}, dim {dim:g})")
    # fg box starts at the full canvas; the iw*P/ih*P scale then shrinks the
    # fitted frame by P percent. Both dims pass through trunc(x/2)*2 -- odd
    # widths/heights would abort the encode at the yuv420p pixel-format stage.
    # overlay=(W-w)/2:(H-h)/2 centres whatever size the fg ends up at.
    filter_complex = (
        f"[0:v]split[a][b];"
        f"[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},eq=brightness=-{dim:g},gblur=sigma={sigma:g}[bg];"
        f"[b]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
        f"scale=trunc(iw*{scale_pct:g}/100/2)*2:trunc(ih*{scale_pct:g}/100/2)*2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", in_path,
        "-filter_complex", filter_complex,
        "-map", "[v]",
    ]
    if _has_audio(in_path):
        cmd += ["-map", "0:a:0?", "-c:a", "copy"]
        log("[clip/local] blurpad: audio stream found, copying")
    else:
        log("[clip/local] blurpad: no audio stream, video only")
    cmd += [
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run_ffmpeg(cmd, log)
    try:
        size = os.path.getsize(out_path)
        log(f"[clip/local] blurpad: done, size={size} bytes -> {out_path}")
    except OSError:
        pass
    return out_path


def apply_blur_padding_for_ar(in_path: str, out_path: str, aspect_ratio: str, log=print) -> str:
    """Aspect-aware wrapper used by the finalize stage: 9:16 gets the blur pass,
    any other ratio is copied through unchanged.

    Copied instead of re-rendered on purpose: a 1:1 draft already has the
    right geometry, and a plain copy is lossless and instant.
    """
    if aspect_ratio == "9:16":
        return apply_blur_padding(in_path, out_path, log=log)
    log(f"[clip/local] blurpad: aspect {aspect_ratio} != 9:16, passthrough copy")
    shutil.copy2(in_path, out_path)
    return out_path
