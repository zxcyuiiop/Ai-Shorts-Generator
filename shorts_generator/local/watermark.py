"""Custom user watermark with a freeze-frame pause, applied at save time.

At second ``at_sec`` the video freezes for ``duration_sec`` seconds (the frame
at ``at_sec`` is looped), the uploaded PNG/logo is scaled to ``scale_pct`` of
the frame width, overlaid centered, and faded in/out over ~0.3s. After the
pause the video resumes. The clip's own audio is replaced by silence for the
pause window; output length = input length + duration_sec.

Implemented as ONE ffmpeg filter_complex pass (same in-place temp +
os.replace house style as title_draw.py):

    [0:v]split=3[vin_pre][vin_after][vin_still]
    [vin_pre]trim=0:at, setpts=PTS-STARTPTS, fps=N, format=yuv420p, setsar=1 [v_pre]
    [vin_still]trim=at:at+0.5, setpts, fps=N, format=yuv420p, setsar=1,
         trim=end_frame=1, loop=loop*fps:1, fps=N, setpts=PTS-STARTPTS [still]
    [1:v]scale=w=W*pct, format=rgba, fade in/out alpha=1 [wm]
    [still][wm]overlay [v_pause]
    [vin_after]trim=start=at, setpts=..., fps=N, format=yuv420p, setsar=1 [v_after]
    [v_pre][a_pre][v_pause][silence][v_after][a_after] concat n=3/a=1

Gotchas encoded here (all learned against ffmpeg 9.0):
  * The freeze frame comes from a THIRD split output, not a second raw [0:v]
    reference: two independent taps on one input stream get frames alternately
    and the loop branch starves — ffmpeg hangs past the timeout.
  * The freeze frame comes from a 0.5s trim window (single-frame trim has no
    decode headroom past one GOP and can land one frame early) —
    fps=N + trim=end_frame=1 then pins it to ONE frame, loop=size=1 repeats it.
  * loop counts FRAMES: loop=int(duration*fps) yields loop+1 total frames —
    one extra source frame (~1/25s too long) — so loop=max(1, frames-1).
  * overlay wants the still stream to carry its own fps; the second fps=N
    after loop is not decoration, without it overlay resyncs oddly.
  * fade alpha=1 must run while the PNG is still rgba (converting to yuv420p
    first quantizes the alpha ramp into visible steps).
  * When the input has no audio stream the audio half of the graph is dropped
    and concat runs with a=0 — generating silence for the whole clip would
    fabricate an audio track the source never had.
  * The endless `-loop 1` watermark input starves the two-way still+overlay
    coupling: give the image an explicit `-t duration_sec` budget instead.
"""
import os
import subprocess
import time

from ..config import env

# Same house style as clipper._run_ffmpeg / title_draw: error-only, bounded.
FFMPEG_TIMEOUT = 180  # seconds

WATERMARK_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

_FADE_SEC = 0.3        # watermark fade in/out length over the frozen frame
_MAX_VIDEO_LEN = 600.0  # sanity ceiling for WATERMARK_AT_SEC


def watermark_enabled() -> bool:
    """WATERMARK_ENABLED ('0' default) master switch — read at use time."""
    return str(env("WATERMARK_ENABLED", "0") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _float(name: str, default: float, lo: float, hi: float) -> float:
    """Env float clamped into [lo, hi]; junk/missing falls back to default."""
    try:
        return max(lo, min(hi, float(env(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _resolve_image(path: str) -> str:
    """Resolve WATERMARK_FILE to an absolute readable path, or raise."""
    raw = (path or "").strip()
    if not raw:
        raise RuntimeError("[watermark] WATERMARK_FILE is not set")
    if not os.path.isabs(raw):
        from .music import PROJECT_ROOT
        raw = os.path.join(PROJECT_ROOT, raw)
    raw = os.path.abspath(raw)
    ext = os.path.splitext(raw)[1].lower()
    if ext not in WATERMARK_EXTENSIONS:
        raise RuntimeError(
            f"[watermark] unsupported image type {ext or '(none)'!r} — "
            "expected png/jpg/webp")
    if not os.path.isfile(raw):
        raise RuntimeError(f"[watermark] file not found: {raw}")
    if not os.access(raw, os.R_OK):
        raise RuntimeError(f"[watermark] file not readable: {raw}")
    if os.path.getsize(raw) <= 0:
        raise RuntimeError(f"[watermark] file is empty: {raw}")
    return raw


def _probe(path: str) -> tuple:
    """(has_audio, duration_seconds, fps, width, height) via ffprobe.

    fps comes only as a parsed r_frame_rate (avg can skew on VFR-but-muxed
    clips); duration from the container first, video stream as fallback.
    Raises RuntimeError when ffprobe is absent or the probe fails.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries",
             "format=duration:stream=codec_type,r_frame_rate,duration,"
             "width,height",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"[watermark] ffprobe failed: {e}")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-4:])
        raise RuntimeError(f"[watermark] ffprobe could not read input:\n{tail}")

    has_audio = False
    duration = 0.0
    fps = 0.0
    width = height = 0
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key == "codec_type":
            if value.strip() == "audio":
                has_audio = True
        elif key == "r_frame_rate" and "/" in value:
            num, _, den = value.strip().partition("/")
            try:
                rate = float(num) / float(den)
                if 1.0 <= rate <= 240.0:
                    fps = rate
            except (ValueError, ZeroDivisionError):
                pass
        elif key == "duration":
            try:
                d = float(value)
                if d > 0 and d > duration:
                    duration = d
            except ValueError:
                pass
        elif key == "width":
            try:
                width = max(width, int(value))
            except ValueError:
                pass
        elif key == "height":
            try:
                height = max(height, int(value))
            except ValueError:
                pass
    return has_audio, duration, fps, width, height


def apply_watermark_pause(in_path: str, out_path: str, image_path: str,
                          at_sec, duration_sec, scale_pct) -> str:
    """Insert a watermark pause into `in_path` and write the result to
    `out_path`. Returns `out_path`.

    Raises RuntimeError on any precondition or ffmpeg failure — the caller
    (finalize stage) wraps this in try/except and keeps the clip either way.
    """
    image_path = _resolve_image(image_path)
    in_path = os.path.abspath(in_path)
    # ffmpeg is run with cwd set to the output directory (house style from
    # title_draw.py), so every path handed to it must be absolute up front.
    out_path = os.path.abspath(out_path)
    if not os.path.isfile(in_path):
        raise RuntimeError(f"[watermark] input video not found: {in_path}")

    try:
        at_sec = float(at_sec)
    except (TypeError, ValueError):
        at_sec = 2.0
    at_sec = max(0.0, min(_MAX_VIDEO_LEN, at_sec))

    has_audio, duration, fps, width, height = _probe(in_path)
    if fps <= 0:
        raise RuntimeError("[watermark] could not determine input frame rate")
    if duration <= 0:
        raise RuntimeError("[watermark] could not determine input duration")
    if duration < 1.0:
        raise RuntimeError(
            f"[watermark] input too short for a pause ({duration:.2f}s)")

    # The freeze must start on a frame that actually exists: beyond the tail
    # clamps to length-0.5s (and keeps the pre-roll non-negative).
    if at_sec > duration - 0.5:
        at_sec = max(0.0, duration - 0.5)

    try:
        duration_sec = float(duration_sec)
    except (TypeError, ValueError):
        duration_sec = 1.5
    duration_sec = max(0.3, min(10.0, duration_sec))

    try:
        scale_pct = float(scale_pct)
    except (TypeError, ValueError):
        scale_pct = 35.0
    scale_pct = max(5.0, min(90.0, scale_pct))
    # Even pixel width: h.264/yuv420p rejects odd dimensions, and an odd
    # watermark width would make scale= fail the whole graph.
    wm_w = max(2, 2 * int(round(width * (scale_pct / 100.0) / 2.0)))

    # Freeze budget in SOURCE frames: loop=size is a count, not a duration.
    # loop=k emits k+1 frames, hence the -1 (see module docstring).
    loop_frames = max(1, int(round(duration_sec * fps)) - 1)
    fade_out_start = max(0.0, duration_sec - _FADE_SEC)

    # Every concat input is pinned to the same fps/geometry/pixel format/SAR —
    # concat refuses mismatched streams, and the still-loop branch is the one
    # that drifts (an odd-height canvas would make scale= produce odd dims).
    v_main = (f"fps={fps:g},scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,setsar=1")
    # One decode, three taps: pre / post / still-grab. A raw second ``[0:v]``
    # reference alongside the split outputs starves the graph — the two taps
    # get frames alternately and the loop branch blocks forever (observed as
    # an ffmpeg hang past the 180 s timeout), so split drives all three.
    fc = (
        f"[0:v]split=3[vin_pre][vin_after][vin_still];"
        f"[vin_pre]trim=0:{at_sec:.6f},setpts=PTS-STARTPTS,{v_main}[v_pre];"
        f"[vin_still]trim=start={at_sec:.6f}:duration=0.5,setpts=PTS-STARTPTS,"
        f"fps={fps:g},scale={width}:{height},format=yuv420p,setsar=1,"
        f"trim=end_frame=1,loop={loop_frames}:1,fps={fps:g},setpts=PTS-STARTPTS[still];"
        f"[1:v]scale=w={wm_w}:h=-1,format=rgba,"
        f"fade=t=in:st=0:d={_FADE_SEC}:alpha=1,"
        f"fade=t=out:st={fade_out_start:.6f}:d={_FADE_SEC}:alpha=1[wm];"
        f"[still][wm]overlay=(W-w)/2:(H-h)/2,format=yuv420p,setsar=1[v_pause];"
        f"[vin_after]trim=start={at_sec:.6f},setpts=PTS-STARTPTS,{v_main}[v_after];"
    )
    if has_audio:
        fc += (
            f"[0:a]atrim=0:{at_sec:.6f},asetpts=PTS-STARTPTS[a_pre];"
            f"anullsrc=r=48000:cl=stereo:d={duration_sec:.6f}[a_gap];"
            f"[0:a]atrim=start={at_sec:.6f},asetpts=PTS-STARTPTS[a_after];"
            f"[v_pre][a_pre][v_pause][a_gap][v_after][a_after]"
            f"concat=n=3:v=1:a=1[v][a]"
        )
    else:
        fc += (
            f"[v_pre][v_pause][v_after]concat=n=3:v=1:a=0[v]"
        )

    # See title_draw/music for the style: ffmpeg runs from the output dir.
    from . import clipper  # deferred: clipper imports this module lazily
    encoder = clipper._get_video_encoder()

    # The watermark image gets an explicit -t budget, not a bare -loop 1.
    # An endless second input here starves the graph: while the still-loop and
    # the overlay wait on frames from each other, ffmpeg keeps consuming the
    # unbounded looped image and never drains the video-tee — the process sits
    # past FFMPEG_TIMEOUT producing nothing (observed on ffmpeg 9.0).
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", in_path,
           "-loop", "1", "-t", f"{duration_sec:.6f}", "-i", image_path,
           "-filter_complex", fc,
           "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]"]
    cmd += clipper._video_encoder_args(encoder)
    if has_audio:
        # The atrim'd wraps around the aac stream are re-encoded whole, so
        # copy is off the table; 192k stereo 48k matches anullsrc's side.
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += ["-r", f"{fps:g}", "-movflags", "+faststart", out_path]

    try:
        print(f"[watermark] freeze at {at_sec:.2f}s +{duration_sec:.2f}s "
              f"(scale {scale_pct:g}%): {os.path.basename(in_path)}", flush=True)
    except UnicodeEncodeError:
        pass  # cosmetic log only; never kill the stage over a print
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", timeout=FFMPEG_TIMEOUT,
                          cwd=os.path.dirname(out_path) or None)
    if proc.returncode != 0:
        # Never leave a partial/empty mp4 where the caller might pick it up.
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:]) \
            or f"exit status {proc.returncode}"
        raise RuntimeError(f"[watermark] ffmpeg failed:\n{tail}")
    try:
        print(f"[watermark] pause applied in {time.time() - start:.2f}s",
              flush=True)
    except UnicodeEncodeError:
        pass
    return out_path
