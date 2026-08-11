"""Tests for the freeze-frame user watermark (shorts_generator/local/watermark.py).

E2E with REAL ffmpeg — the same hermetic pattern as test_title_draw.py:

1. A tiny 3s 320x180 testsrc+sine clip is generated, a 60x40 red RGBA logo is
   drawn with Pillow, and the real watermark stage runs on it.
2. Output duration must grow by exactly the pause (3s + 1s ~= 4s), geometry
   and the audio track must survive.
3. A frame decoded mid-pause (t=1.5s of the output) must carry the red logo
   at the center — the freeze doubles the frame at at=1.0s, so we also check
   that the paused frame matches the source frame at the freeze point (a
   "playing" clip would have rolled past it by then).
4. Edge cases: an `at` past the tail clamps instead of crashing; a missing
   image raises RuntimeError (the caller treats it as "skip the stage").
5. Video banner: a 1s mp4 with sound replaces the logo — the pause follows
   the banner's length (not the duration knob), its soundtrack fills the gap,
   its picture is centered, and the banner animates while the clip is frozen.

Hermetic: settings.local.json is pointed into a temp dir BEFORE any
shorts_generator import so the persisted file can never leak machine settings
into the run (and vice versa). Skips cleanly when ffmpeg/ffprobe are missing.
FORCE_CPU_FFMPEG keeps the encoder pick deterministic off-GPU.
"""
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not installed; watermark E2E skipped")
    sys.exit(0)

try:
    from PIL import Image  # noqa: F401
except ImportError:
    print("SKIP: Pillow not installed; watermark E2E skipped")
    sys.exit(0)

_TMP = tempfile.mkdtemp(prefix="watermark-")

# Neutralize the settings layer BEFORE importing anything that reads it.
from shorts_generator import settings_store  # noqa: E402
settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

from shorts_generator.local import watermark as _wm  # noqa: E402


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          f"{(' - ' + detail) if detail else ''}", flush=True)
    return [] if cond else [name]


def probe_duration(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, errors="replace", timeout=30)
    return float(proc.stdout.strip())


def probe_dims(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, errors="replace", timeout=30)
    w, h = proc.stdout.strip().splitlines()[-1].split("x")[:2]
    return int(w), int(h)


def probe_has_audio(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True, errors="replace", timeout=30)
    return "audio" in proc.stdout


def frame_rgb(path, at):
    """Decode one frame at `at` seconds as an HxWx3 uint8 array."""
    import numpy as np
    w, h = probe_dims(path)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", path,
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "ppm", "-"],
        capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"frame decode failed for {os.path.basename(path)}")
    data = proc.stdout
    # Parse the P6 header: "P6 w h max" then ONE whitespace byte, then pixels.
    tokens = []
    i = 0
    while len(tokens) < 4:
        while data[i:i+1].isspace():
            i += 1
        start = i
        while i < len(data) and not data[i:i+1].isspace():
            i += 1
        tokens.append(data[start:i])
    i += 1  # the single whitespace separator before the binary pixel data
    w_hdr, h_hdr = int(tokens[1]), int(tokens[2])
    assert (w_hdr, h_hdr) == (w, h), f"P6 dims {w_hdr}x{h_hdr} != probe {w}x{h}"
    arr = np.frombuffer(data[i:], dtype=np.uint8, count=w * h * 3)
    return arr[: w * h * 3].reshape(h, w, 3)


def _build_fixtures(tmpdir):
    """3s 320x180@15 clip with a tone, and a semi-transparent red logo."""
    src = os.path.join(tmpdir, "src.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=3:size=320x180:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", src],
        check=True, capture_output=True, timeout=60)
    logo = os.path.join(tmpdir, "logo.png")
    Image.new("RGBA", (60, 40), (255, 0, 0, 200)).save(logo)
    return src, logo


def _build_video_banner(tmpdir):
    """1s 160x90@15 solid-blue testsrc banner with a loud 880Hz tone.

    testsrc is used (not a lavfi color source: ffmpeg rejects
    color=...:rate= with a generic init error) and negated/darkened so the
    overlay is trivially distinguishable from the main clip's picture.
    """
    banner = os.path.join(tmpdir, "banner.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x90:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
         "-vf", "negate,eq=brightness=-0.3",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", banner],
        check=True, capture_output=True, timeout=60)
    return banner


def audio_mean(path, ss, dur):
    """Mean absolute PCM level of the audio window [ss, ss+dur)."""
    import numpy as np
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}",
         "-i", path, "-vn", "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
        capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"audio decode failed for {os.path.basename(path)}")
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    return float(np.abs(pcm).mean())


def e2e_checks():
    fails = []
    tmpdir = tempfile.mkdtemp(prefix="watermark_stage_", dir=_TMP)
    try:
        from PIL import Image
        import numpy as np

        src, logo = _build_fixtures(tmpdir)
        fails += check("source 320x180/3s generated",
                       probe_dims(src) == (320, 180)
                       and abs(probe_duration(src) - 3.0) < 0.5)

        out = os.path.join(tmpdir, "watermarked.mp4")
        os.environ["FORCE_CPU_FFMPEG"] = "1"
        try:
            _wm.apply_watermark_pause(src, out, logo, 1.0, 1.0, 35)
        finally:
            os.environ.pop("FORCE_CPU_FFMPEG", None)

        fails += check("watermarked clip exists", os.path.isfile(out))
        fails += check("output = input + pause (±0.5s)",
                       abs(probe_duration(out) - 4.0) <= 0.5,
                       f"3.00s -> {probe_duration(out):.2f}s")
        fails += check("geometry preserved", probe_dims(out) == (320, 180))
        fails += check("audio track survives the pause", probe_has_audio(out))

        # Mid-pause frame carries the logo: testsrc never paints pure red at
        # the dead center, so red-dominant center = overlay went in.
        mid = frame_rgb(out, at=1.5)
        h, w = mid.shape[:2]
        patch = mid[h // 2 - 5:h // 2 + 5, w // 2 - 5:w // 2 + 5].astype(np.float32)
        r, g, b = patch[..., 0].mean(), patch[..., 1].mean(), patch[..., 2].mean()
        fails += check("mid-pause frame carries the red logo",
                       r > g + 60 and r > b + 60,
                       f"center RGB=({r:.0f},{g:.0f},{b:.0f})")

        # Freeze proof, without tripping on encoder lookahead: a fade at the
        # pause boundary makes x264 blend the transition, so an absolute diff
        # against the origin frame is unreliable. Instead require SOME pair of
        # consecutive frames INSIDE the pause window to be identical (a looped
        # still repeats exactly; a playing testsrc never does).
        pause_times = (1.35, 1.50, 1.65)
        pause_frames = [frame_rgb(out, at=t).astype(np.float32) for t in pause_times]
        consec = [float(np.abs(b - a).mean())
                  for a, b in zip(pause_frames, pause_frames[1:])]
        fails += check("picture freezes during the pause (consecutive frames repeat)",
                       min(consec) < 0.5,
                       f"min consecutive delta={min(consec):.2f}")

        # After the pause the video must resume rolling: consecutive frames
        # past the window must differ again (the clip is moving once more).
        post_frames = [frame_rgb(out, at=t).astype(np.float32) for t in (2.5, 2.7, 2.9)]
        post = [float(np.abs(b - a).mean())
                for a, b in zip(post_frames, post_frames[1:])]
        fails += check("video resumes after the pause",
                       min(post) > 0.5,
                       f"post-pause consecutive deltas min={min(post):.2f}")

        # Edge: `at` beyond the tail clamps to near the end instead of dying.
        tail = os.path.join(tmpdir, "tail.mp4")
        os.environ["FORCE_CPU_FFMPEG"] = "1"
        try:
            _wm.apply_watermark_pause(src, tail, logo, 99.0, 1.0, 35)
        finally:
            os.environ.pop("FORCE_CPU_FFMPEG", None)
        fails += check("at beyond the tail clamps, output still valid",
                       abs(probe_duration(tail) - 4.0) <= 0.5
                       and probe_dims(tail) == (320, 180),
                       f"duration={probe_duration(tail):.2f}s")

        # Edge: a missing image is a hard RuntimeError — the caller logs
        # "[watermark] skipped" and keeps the clip, so it must raise, not
        # hang or write junk.
        try:
            _wm.apply_watermark_pause(src, os.path.join(tmpdir, "x.mp4"),
                                      os.path.join(tmpdir, "missing.png"),
                                      1.0, 1.0, 35)
            fails += check("missing image raises RuntimeError", False,
                           "no exception raised")
        except RuntimeError:
            fails += check("missing image raises RuntimeError", True)
        except Exception as e:
            fails += check("missing image raises RuntimeError", False,
                           f"wrong type: {type(e).__name__}: {e}")

        # Video watermark: the banner's own length rules the pause, and its
        # soundtrack replaces the silence in the gap.
        banner = _build_video_banner(tmpdir)
        fails += check("video banner generated (1s, with sound)",
                       abs(probe_duration(banner) - 1.0) < 0.5
                       and probe_has_audio(banner))

        vout = os.path.join(tmpdir, "video_watermarked.mp4")
        os.environ["FORCE_CPU_FFMPEG"] = "1"
        try:
            _wm.apply_watermark_pause(src, vout, banner, None, 9.9, 60)
        finally:
            os.environ.pop("FORCE_CPU_FFMPEG", None)

        fails += check("video-watermarked clip exists", os.path.isfile(vout))
        fails += check("pause length = banner length (banner wins over the knob)",
                       abs(probe_duration(vout) - 4.0) <= 0.6,
                       f"3.00s + 1s banner -> {probe_duration(vout):.2f}s")
        fails += check("geometry preserved (video banner)",
                       probe_dims(vout) == (320, 180))
        fails += check("audio track survives (video banner)",
                       probe_has_audio(vout))

        # at=None centers the pause: at = 1.5 - 0.5 = 1.0s, so the banner
        # covers [1.0, 2.0). Its audio must fill that exact window...
        mid_gap_audio = audio_mean(vout, 1.2, 0.5)
        fails += check("banner soundtrack plays during the pause",
                       mid_gap_audio > 200.0,
                       f"gap audio mean={mid_gap_audio:.0f}")
        # ...and the clip's own 440Hz tone resumes afterwards.
        post_audio = audio_mean(vout, 2.6, 0.5)
        fails += check("clip audio resumes after the pause",
                       post_audio > 200.0,
                       f"post-pause audio mean={post_audio:.0f}")

        # Mid-pause frame carries the banner: compare the center patch
        # (covered by the 60%-wide overlay) against a corner patch far
        # outside it — the frozen testsrc survives in the corners, so the
        # banner's negated/darkened picture must differ from it markedly.
        vmid = frame_rgb(vout, at=1.5)
        h, w = vmid.shape[:2]
        vcenter = vmid[h // 2 - 5:h // 2 + 5,
                       w // 2 - 5:w // 2 + 5].astype(np.float32)
        vcorner = vmid[2:12, 2:12].astype(np.float32)
        vdiff = float(np.abs(vcenter - vcorner).mean())
        fails += check("video banner visible mid-pause (center != corner)",
                       vdiff > 25.0, f"center-vs-corner delta={vdiff:.1f}")

        # The banner ANIMATES over the frozen frame: two frames inside the
        # pause window must differ (the still underneath repeats exactly, so
        # any movement is the banner — unlike the still-image freeze test).
        vanim = [frame_rgb(vout, at=t).astype(np.float32) for t in (1.2, 1.6)]
        vanim_delta = float(np.abs(vanim[1] - vanim[0]).mean())
        fails += check("video banner animates during the pause",
                       vanim_delta > 0.5,
                       f"in-pause frame delta={vanim_delta:.2f}")
    except Exception as e:
        print(f"ERROR  {type(e).__name__}: {e}", flush=True)
        fails.append(f"e2e exception: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return fails


def main():
    failures = []
    failures += e2e_checks()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
