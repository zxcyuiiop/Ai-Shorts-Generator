"""Blur-padding to 9:16 vertical with blurred bars top/bottom.

Instead of hard-cropping the frame to a tall 9:16 window, this renders:
  * background: the video scaled to fill OUT_W x OUT_H (1080x1920), blurred
    with gblur sigma=BLUR_SIGMA,
  * foreground: the video scaled to FG_H (1344) tall keeping aspect, then
    centre-cropped to OUT_W wide -- only the LEFT/RIGHT sides are cut
    (nothing is taken off the top/bottom for sources at least as wide as
    the output box),
  * overlaid at y=BAR_H (288), i.e. 288 px blurred bar top + 288 px bottom.

Geometry, all derived from BAR_PERCENT at import time:
  OUT_W  = 1080                     -- output width
  OUT_H  = 1920                     -- output height
  BAR_PERCENT = 15                  -- each bar is 15% of OUT_H
  BAR_H  = OUT_H * 15 / 100  = 288  -- one bar's height (top and bottom)
  FG_H   = OUT_H * 70 / 100  = 1344 -- foreground strip height
  BLUR_SIGMA = 20                   -- gblur sigma for the background

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
import subprocess

from ..config import env

# --- Geometry constants (BAR_PERCENT is the single source of truth) ---------
BAR_PERCENT = 15          # each blurred bar takes this % of the output height
OUT_W = 1080              # output width
OUT_H = 1920              # output height
BAR_H = OUT_H * BAR_PERCENT // 100                  # 288 px per bar
FG_H = OUT_H * (100 - 2 * BAR_PERCENT) // 100       # 1344 px foreground
BLUR_SIGMA = 20           # gblur sigma for the background layer


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
    """Render in_path onto a blurred 1080x1920 canvas in one ffmpeg pass.

    Foreground: scaled to FG_H tall keeping aspect, centre-cropped to OUT_W
    wide (sides only are cut for wide sources), overlaid at y=BAR_H so the
    blurred background shows as 288 px bars top and bottom.
    Audio is stream-copied when present, omitted otherwise.
    Returns out_path.
    """
    log(f"[clip/local] blurpad: {in_path} -> {out_path} "
        f"({OUT_W}x{OUT_H}, bars 2x{BAR_H}px, fg {OUT_W}x{FG_H}, blur {BLUR_SIGMA})")
    # scale=OUT_W:FG_H:force_original_aspect_ratio=increase + crop is the
    # variant that works for BOTH wide and narrow sources: it scales until the
    # frame covers the OUT_W x FG_H box (width always >= OUT_W), so the centre
    # crop never sees a frame narrower than OUT_W. The naive scale=-2:FG_H can
    # yield width < 1080 for narrow sources and crop would error out.
    filter_complex = (
        f"[0:v]split[a][b];"
        f"[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},gblur=sigma={BLUR_SIGMA}[bg];"
        f"[b]scale={OUT_W}:{FG_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{FG_H}[fg];"
        f"[bg][fg]overlay=(W-w)/2:{BAR_H}[v]"
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
