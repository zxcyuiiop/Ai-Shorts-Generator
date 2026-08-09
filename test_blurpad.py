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

Everything is stubbed: subprocess.run (ffprobe probe + ffmpeg render) and
os.path.getsize are replaced -- no real ffmpeg, no real video.
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
        check("default sigma 25", "gblur=sigma=25" in filt, filt)
        check("default dim 0.15 before blur",
              "eq=brightness=-0.15,gblur=sigma=25" in filt, filt)

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
