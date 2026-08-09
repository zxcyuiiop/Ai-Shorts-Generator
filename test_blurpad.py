"""Checks for the blurred-background fit redesign in local/blurpad.py.

Covers:
  - fg uses force_original_aspect_ratio=decrease (whole frame, NO crop) and a
    centred overlay=(W-w)/2:(H-h)/2
  - bg chain is cover-scale + crop + eq dim + gblur, dim applied BEFORE blur
  - BLURPAD_FG_SCALE / BLURPAD_SIGMA / BLURPAD_DIM env knobs change the filter
  - clamp edges: fg scale 50..100, dim 0..0.7 (negative dim would brighten the
    bg -- eq=brightness=--0.5 is invalid ffmpeg, so it must clamp to 0)
  - blurpad_enabled_for() master-switch matrix (unchanged behavior)
  - apply_blur_padding_for_ar(): 9:16 -> blur pass, other ratios -> copy2
  - request-level overrides: set_overrides({"BLUR_BARS": "0"}) must DROP the
    blur pass in finalize_clip_local even while settings.local.json says 1
    (this is the regression that shipped: set_overrides used to discard falsy
    values, so env() fell through to the persisted settings file)
  - E2E (only when ffmpeg+PIL are available): render a real 1080x1920 clip
    and check the top/bottom ~25% bands are non-black and visibly blurrier
    than the centre (Laplacian-variance heuristic).

The stubbed part replaces subprocess.run (ffprobe probe + ffmpeg render) and
os.path.getsize -- no real ffmpeg, no real video there.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Neutralize the settings layer BEFORE importing anything that reads it:
# blurpad reads BLUR_BARS through config.env(), which consults the real
# settings.local.json -- a saved blur_bars=1 there would shadow the
# os.environ values this test flips below.
_TMP = tempfile.mkdtemp(prefix="blurpad-settings-")
from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

from shorts_generator.local import blurpad as bp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class SkipTest(Exception):
    """Optional dependency missing -- the caller reports a SKIP line."""


def capture_filter(has_audio=True):
    """Run apply_blur_padding with stubbed subprocess/getsize; return (filter, cmd)."""
    calls = []
    real_run, real_getsize = bp.subprocess.run, bp.os.path.getsize
    try:
        bp.subprocess.run = lambda *a, **k: (
            calls.append(a[0]) or
            FakeProc(stdout="audio\n" if a[0][0] == "ffprobe" and has_audio else "")
        )
        bp.os.path.getsize = lambda p: 12345
        logs = []
        ret = bp.apply_blur_padding("in.mp4", "out.mp4", log=logs.append)
        assert ret == "out.mp4", "must return out_path"
        ff = next(c for c in calls if c[0] == "ffmpeg")
        filt = ff[ff.index("-filter_complex") + 1]
        return filt, ff, logs
    finally:
        bp.subprocess.run, bp.os.path.getsize = real_run, real_getsize


ENV_KEYS = ("BLURPAD_FG_SCALE", "BLURPAD_SIGMA", "BLURPAD_DIM", "BLUR_BARS")


def clear_env():
    for k in ENV_KEYS:
        os.environ.pop(k, None)


def e2e_blur_bands(tmp):
    """Real ffmpeg render: 640x360 source -> 1080x1920 with blurred bands.

    Verifies the bars the user actually sees: the output is 1080x1920, the
    top/bottom ~25% bands are not black (the dim never crushes them), and they
    are visibly blurrier than the sharp foreground centre.
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        raise SkipTest("PIL not installed")
    import subprocess

    src = os.path.join(tmp, "src_e2e.mp4")
    out = os.path.join(tmp, "out_e2e.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
         "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", src],
        check=True, timeout=120)

    clear_env()  # defaults: fg 100%, sigma 18, dim 0.06
    bp.apply_blur_padding(src, out, log=lambda s: None)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", out],
        capture_output=True, text=True, timeout=30)
    check("e2e: output is 1080x1920", probe.stdout.strip() == "1080,1920",
          probe.stdout.strip())

    frame = os.path.join(tmp, "frame_e2e.png")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", "1.0", "-i", out, "-frames:v", "1", frame],
        check=True, timeout=60)
    img = Image.open(frame).convert("L")
    check("e2e: frame decodes at 1080x1920", img.size == (1080, 1920), str(img.size))
    w, h = img.size
    band = int(h * 0.25)
    top = img.crop((0, 0, w, band))
    bottom = img.crop((0, h - band, w, h))
    center = img.crop((int(w * 0.2), int(h * 0.45), int(w * 0.8), int(h * 0.55)))

    def stats(region):
        px = list(region.getdata())
        mean = sum(px) / len(px)
        # Laplacian-variance sharpness: blur the region, then measure how much
        # the 3x3 sharpening kernel still changes it (PIL has no raw Laplacian;
        # FIND_EDGES responds the same way -- soft blur -> weak response).
        edges = region.filter(ImageFilter.FIND_EDGES)
        epx = list(edges.getdata())
        emean = sum(epx) / len(epx)
        var = sum((v - emean) ** 2 for v in epx) / len(epx)
        return mean, var

    top_mean, top_sharp = stats(top)
    bot_mean, bot_sharp = stats(bottom)
    ctr_mean, ctr_sharp = stats(center)
    detail = (f"top(mean={top_mean:.0f},sharp={top_sharp:.0f}) "
              f"bottom(mean={bot_mean:.0f},sharp={bot_sharp:.0f}) "
              f"centre(mean={ctr_mean:.0f},sharp={ctr_sharp:.0f})")
    check("e2e: top band not black (mean > 8)", top_mean > 8, detail)
    check("e2e: bottom band not black (mean > 8)", bot_mean > 8, detail)
    check("e2e: bands blurrier than centre",
          top_sharp < ctr_sharp * 0.5 and bot_sharp < ctr_sharp * 0.5, detail)


def main():
    tmp = tempfile.mkdtemp(prefix="blurpad-test-")
    clear_env()
    try:
        # --- filter shape: decrease-scale fg, no fg crop, centred overlay -----
        filt, cmd, logs = capture_filter()
        check("fg uses decrease aspect scale",
              f"scale={bp.OUT_W}:{bp.OUT_H}:force_original_aspect_ratio=decrease" in filt, filt)
        check("no crop on fg branch", "decrease,crop" not in filt and "[fg]" in filt, filt)
        check("even-dims fg pre-shrink present", "trunc(iw*100/100/2)*2" in filt, filt)
        check("centred overlay (W-w)/2:(H-h)/2", "overlay=(W-w)/2:(H-h)/2" in filt, filt)
        check("default sigma 18", "gblur=sigma=18" in filt, filt)
        check("default dim 0.06 before blur",
              "eq=brightness=-0.06,gblur=sigma=18" in filt, filt)

        # bg branch: increase-scale then crop (the cover trick), only the bg crops
        # filter segments: [0:v]split / [a]...=[bg] / [b]...=[fg] / [bg][fg]overlay...
        bg_branch = filt.split(";")[1]
        check("bg cover-scales with increase",
              "force_original_aspect_ratio=increase" in bg_branch, bg_branch)
        check("bg crops to canvas", f"crop={bp.OUT_W}:{bp.OUT_H}" in bg_branch, bg_branch)
        check("single ffmpeg pass maps [v]", cmd[cmd.index("-map") + 1] == "[v]", str(cmd))
        check("audio copied by default stub",
              "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy", str(cmd))

        filt_noaudio, cmd_noaudio, _ = capture_filter(has_audio=False)
        check("no-audio input: no audio map/codec",
              "-c:a" not in cmd_noaudio and "0:a:0?" not in cmd_noaudio, str(cmd_noaudio))

        # --- env knobs change the filter --------------------------------------
        os.environ["BLURPAD_FG_SCALE"] = "80"
        os.environ["BLURPAD_SIGMA"] = "40"
        os.environ["BLURPAD_DIM"] = "0.4"
        filt2, _, _ = capture_filter()
        check("FG_SCALE=80 shrinks fg box", "trunc(iw*80/100/2)*2" in filt2, filt2)
        check("SIGMA=40 reaches gblur", "gblur=sigma=40" in filt2, filt2)
        check("DIM=0.4 reaches eq", "eq=brightness=-0.4" in filt2, filt2)

        # --- clamps ------------------------------------------------------------
        os.environ["BLURPAD_FG_SCALE"] = "10"     # below 50 -> clamp
        os.environ["BLURPAD_DIM"] = "5"           # above 0.7 -> clamp
        filt3, _, _ = capture_filter()
        check("FG_SCALE clamps up to 50", "trunc(iw*50/100/2)*2" in filt3, filt3)
        check("DIM clamps down to 0.7", "eq=brightness=-0.7" in filt3, filt3)

        os.environ["BLURPAD_FG_SCALE"] = "250"    # above 100 -> clamp
        os.environ["BLURPAD_DIM"] = "-0.5"        # negative -> clamp to 0
        filt4, _, _ = capture_filter()
        check("FG_SCALE clamps down to 100", "trunc(iw*100/100/2)*2" in filt4, filt4)
        check("DIM clamps up to 0 (never brightens)",
              "eq=brightness=-0," in filt4 or "eq=brightness=0" in filt4, filt4)

        os.environ["BLURPAD_FG_SCALE"] = "banana"  # invalid -> default
        filt5, _, _ = capture_filter()
        check("invalid FG_SCALE falls back to 100", "trunc(iw*100/100/2)*2" in filt5, filt5)
        clear_env()

        # --- enabled_for matrix ------------------------------------------------
        clear_env()
        check("enabled_for default: 9:16 on", bp.blurpad_enabled_for("9:16") is True)
        for raw in ("0", "false", "no", "FALSE"):
            os.environ["BLUR_BARS"] = raw
            check(f"enabled_for BLUR_BARS={raw}: off",
                  bp.blurpad_enabled_for("9:16") is False)
        os.environ["BLUR_BARS"] = "1"
        check("enabled_for: 1:1 always off", bp.blurpad_enabled_for("1:1") is False)
        check("enabled_for: 16:9 always off", bp.blurpad_enabled_for("16:9") is False)
        clear_env()

        # --- for_ar wrapper ----------------------------------------------------
        src = os.path.join(tmp, "src.mp4")
        dst = os.path.join(tmp, "dst.mp4")
        with open(src, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")

        # 9:16 routes into apply_blur_padding
        real_apply = bp.apply_blur_padding
        calls = []
        try:
            bp.apply_blur_padding = lambda i, o, log=print: calls.append((i, o)) or o
            r = bp.apply_blur_padding_for_ar(src, dst, "9:16", log=lambda s: None)
            check("for_ar 9:16 -> blur pass", calls == [(src, dst)] and r == dst, str(calls))
        finally:
            bp.apply_blur_padding = real_apply

        # other ratios passthrough via copy2 (content preserved, no ffmpeg hit)
        real_run = bp.subprocess.run
        try:
            bp.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("subprocess must not run for passthrough"))
            for ar in ("1:1", "16:9", "4:5"):
                if os.path.exists(dst):
                    os.remove(dst)
                r = bp.apply_blur_padding_for_ar(src, dst, ar, log=lambda s: None)
                with open(dst, "rb") as f:
                    copied = f.read()
                check(f"for_ar {ar} -> copy2 passthrough",
                      r == dst and copied == b"\x00\x00\x00\x18ftypmp42", f"bytes={copied!r}")
        finally:
            bp.subprocess.run = real_run

        # --- request overrides must gate the blur pass -------------------------
        # The save/finalize endpoints rebuild the job's GUI params and call
        # config.set_overrides() before finalize_clip_local. Regression: the old
        # set_overrides dropped falsy values, so blur_bars="0" from the request
        # never reached env() and the persisted settings file won instead.
        from shorts_generator.config import clear_overrides, set_overrides
        from shorts_generator.local import clipper as _clip

        draft = os.path.join(tmp, "draft.mp4")
        with open(draft, "wb") as f:
            f.write(b"draft")
        enabled_calls = []

        def fake_apply(in_path, out_path, log=print):
            enabled_calls.append((in_path, out_path))
            with open(out_path, "wb") as f:
                f.write(b"blurred")
            return out_path

        real_apply, real_captions = bp.apply_blur_padding, _clip.captions_enabled
        try:
            _clip.apply_blur_padding = fake_apply
            _clip.captions_enabled = lambda: False
            # settings layer is the empty scratch file (redirected above) and
            # BLUR_BARS is unset -> the builtin default '1' must switch blur ON.
            # OVERLAY_ENABLED=0 keeps the TikTok stage from opening our fake mp4.
            set_overrides({"OVERLAY_ENABLED": "0"})
            try:
                _clip.finalize_clip_local(draft, "9:16")
            finally:
                clear_overrides()
            check("finalize: default '1' (no blur override) runs the blur pass",
                  enabled_calls and enabled_calls[0][0].endswith(".prerender.mp4")
                  and enabled_calls[0][1] == draft,
                  f"calls={enabled_calls}")
            with open(draft, "rb") as f:
                check("finalize: blur output swapped back onto the draft path",
                      f.read() == b"blurred")
            check("finalize: prerender temp cleaned up",
                  not os.path.exists(draft + ".prerender.mp4"))

            enabled_calls.clear()
            # user unchecked the box in the GUI (+ overlay still off)
            set_overrides({"BLUR_BARS": "0", "OVERLAY_ENABLED": "0"})
            try:
                _clip.finalize_clip_local(draft, "9:16")
            finally:
                clear_overrides()
            check("finalize: override BLUR_BARS=0 skips the blur pass",
                  not enabled_calls, f"calls={enabled_calls}")
        finally:
            _clip.apply_blur_padding = real_apply
            _clip.captions_enabled = real_captions

        # --- E2E: real ffmpeg render -> 1080x1920, non-black blurred bands ----
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            try:
                e2e_blur_bands(tmp)
            except SkipTest as e:
                print(f"SKIP  real-render checks - {e}")
    finally:
        clear_env()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
