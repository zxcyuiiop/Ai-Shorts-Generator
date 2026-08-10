"""Manual e2e check: /api/shorts/save END-TO-END blur render.

Generates a synthetic landscape source, walks it through the REAL save path
(copy and paste of app.py save_short: skip reframe for 9:16+blur, then
finalize_clip_local), then measures the rendered frame's luminance the same
way the earlier manual verification did (bars vs foreground).

Run: venv/Scripts/python.exe test_save_blur_e2e.py
"""

import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "output", "uploads", "_save_e2e_blur")
os.makedirs(OUT, exist_ok=True)
SRC = os.path.join(OUT, "orig_16x9.mp4")
DRAFT = os.path.join(OUT, "clip_01.mp4")
CUTF = os.path.join(OUT, "clip_01.mp4.cut.mp4")
FINAL = os.path.join(OUT, "clip_01.final.mp4")

passed = []


def check(name, cond, info=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status}  {name} - {info}")
    passed.append(cond)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2000:])
        raise SystemExit(f"command failed: {cmd}")
    return p


# --- 1. synthetic landscape source: bright noisy content so blur is measurable
if not os.path.isfile(SRC):
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         "testsrc2=size=1920x1080:rate=30:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", SRC])
shutil.copy2(SRC, DRAFT)   # pretend this is the 16:9 draft
check("setup: draft exists", os.path.isfile(DRAFT), DRAFT)
# pretend the pipeline left its cut file (source discovery candidate #1)
shutil.copy2(SRC, CUTF)

# --- 2. real save path, real code -------------------------------------------
from shorts_generator.config import set_overrides
set_overrides({"BLUR_BARS": "1", "BLURPAD_DIM": "0.5", "BLURPAD_SIGMA": "22"})

from shorts_generator.local.blurpad import blurpad_enabled_for
from shorts_generator.local.clipper import _reframe_vertical, finalize_clip_local

aspect = "9:16"
skip_reframe = aspect == "9:16" and blurpad_enabled_for("9:16")
check("skip_reframe is True for 9:16 + blur", skip_reframe)
tmp = DRAFT + ".tmp_save.mp4"
if skip_reframe:
    shutil.copy2(DRAFT, tmp)
else:
    _reframe_vertical(DRAFT, tmp, aspect)
t0 = time.time()
finalize_clip_local(tmp, aspect)
print(f"[e2e] finalize took {time.time()-t0:.1f}s")
shutil.move(tmp, FINAL)

# --- 3. measure the rendered frame ------------------------------------------
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0",
     "-show_entries", "stream=width,height", "-of", "csv=p=0", FINAL],
    capture_output=True, text=True)
w, h = (int(x) for x in probe.stdout.strip().split(","))
check("final is 1080x1920", (w, h) == (1080, 1920), f"{w}x{h}")


def ymean(ss, x0, y0, w0, h0):
    """Mean luma of a crop of the frame at ``ss`` seconds."""
    p = subprocess.run(
        ["ffmpeg", "-ss", f"{ss}", "-i", FINAL, "-vf",
         f"crop={w0}:{h0}:{x0}:{y0},signalstats,"
         "metadata=print:key=lavfi.signalstats.YAVG",
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True)
    for line in p.stderr.splitlines():
        if "YAVG=" in line:
            # line: lavfi.signalstats.YAVG=126.991
            return float(line.strip().rsplit("=", 1)[-1])
    return None


# fg occupies 1080x608 around y=656..1264; bars above/below (fg_scale=100%)
fg = ymean(1.0, 0, 800, 1080, 400)
top = ymean(1.0, 0, 60, 1080, 300)
bot = ymean(1.0, 0, 1660, 1080, 240)
b2f_top = top / fg
b2f_bot = bot / fg
print(f"[e2e] fg={fg:.1f} top={top:.1f} bottom={bot:.1f} "
      f"bars/fg={b2f_top:.2f}/{b2f_bot:.2f}")
check("bars clearly darker than fg", b2f_top < 0.6 and b2f_bot < 0.6,
      f"{b2f_top:.2f}/{b2f_bot:.2f}")
check("bars not black (blur visible)", b2f_top > 0.15 and b2f_bot > 0.15,
      f"{b2f_top:.2f}/{b2f_bot:.2f}")

# cleanup helpers: keep FINAL so the run can be eyeballed, ditch temp inputs
for junk in (DRAFT, CUTF):
    try:
        os.remove(junk)
    except OSError:
        pass

print()
if all(passed):
    print("All checks passed.")
else:
    print(f"FAILED: {passed.count(False)} check(s)")
    sys.exit(1)
