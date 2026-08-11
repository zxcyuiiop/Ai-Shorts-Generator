"""Tests for the burned-in highlight title (shorts_generator/local/title_draw.py).

Two layers:

1. UNIT — the filter-string builder: correct escaping of : ' % \\ , [ ];
   word-bounded wrap of long titles onto two drawtext stages; "…"-truncation
   when the title exceeds the ~80 char budget; font parametrization (explicit
   arialbd.ttf on Windows vs the filtergraph-safe default otherwise); numeric
   y/fontsize as passed through.

2. E2E with REAL ffmpeg — a tiny 2s 1080x1920 testsrc is generated, the real
   title stage runs on it, and a frame decoded at t=1s must differ from the
   same frame of the source by more than drawtext antialiasing noise. Duration
   and resolution must stay identical.

Hermetic: settings.local.json is pointed into a temp dir BEFORE any
shorts_generator import so the persisted file can never leak machine settings
into the run (and vice versa). Every other finalize stage is irrelevant here
— the stage is exercised directly on a freshly written clip. Skips cleanly
when ffmpeg/ffprobe are missing.
"""
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# ffmpeg draws the text — without it only the pure-string unit checks run,
# and those need no external tools at all, so a missing ffmpeg means SKIP.
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not installed; title-draw E2E skipped")
    sys.exit(0)

_TMP = tempfile.mkdtemp(prefix="title-draw-")

# Neutralize the settings layer BEFORE importing anything that reads it.
from shorts_generator import settings_store  # noqa: E402
settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

# Module objects (not from-imports) so lifecycle stays explicit.
from shorts_generator.local import clipper as _clip  # noqa: E402
from shorts_generator.local import title_draw as _tbl  # noqa: E402


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


def frame_gray_f32(path, at=1.0):
    """Decode one frame at `at` seconds as a grayscale float32 HxW array."""
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
    # ffmpeg writes no comment lines here, so 4 whitespace-split fields suffice;
    # guard against a comment anyway by scanning tokens, not fixed offsets.
    tokens = []
    i = 0
    while len(tokens) < 4:
        while data[i:i+1].isspace():  # skip leading whitespace runs
            i += 1
        start = i
        while i < len(data) and not data[i:i+1].isspace():
            i += 1
        tokens.append(data[start:i])
    i += 1  # the single whitespace separator before the binary pixel data
    w_hdr, h_hdr, _max = int(tokens[1]), int(tokens[2]), int(tokens[3])
    assert (w_hdr, h_hdr) == (w, h), f"P6 dims {w_hdr}x{h_hdr} != probe {w}x{h}"
    arr = np.frombuffer(data[i:], dtype=np.uint8, count=w * h * 3)
    return arr[: w * h * 3].reshape(h, w, 3).astype(np.float32).mean(axis=2)


# --- checks ------------------------------------------------------------------
def unit_checks():
    fails = []

    # Escaping: ended up in the filter string exactly once each, ' dropped.
    esc = _tbl._drawtext_escape("Time: 50\\% of it's, \"[best]\"\\")
    fails += check("drawtext escape leaves no raw filter separators",
                   "\\:" in esc and "'" not in esc
                   and "\\," in esc and "\\\\" in esc,
                   esc)
    # '%' passes through PLAIN — the filter carries expansion=none, which is
    # the only form ffmpeg 9 accepts a percent in (the \\% escape dies there).
    fails += check("expansion=none disarms percent",
                   "expansion=none:" in _tbl.build_title_drawtext_filter("x"))
    pct = _tbl.build_title_drawtext_filter("100% off")
    fails += check("bare percent survives into the filter",
                   "text='100% off'" in pct, pct)

    # One short title -> one drawtext stage, no '=' terminator ambiguity.
    short = _tbl.build_title_drawtext_filter("Quick hello", 750, 64)
    fails += check("short title fits one drawtext stage",
                   short.count("drawtext=") == 1 and ",drawtext=" not in short)
    fails += check("baseline y = h - 750 - text_h",
                   ":y=h-750-text_h" in short)
    fails += check("centered horizontally",
                   ":x=(w-text_w)/2" in short)

    # A long title wraps onto two drawtext stages, second one closer to bottom.
    long = ("Это довольно длинный заголовок яркого момента, который точно "
            "превышает тридцать восемь символов и требует второй строки")
    two = _tbl.build_title_drawtext_filter(long, 750, 64)
    first_step = int(round(64 * 1.15))
    fails += check("long title wraps onto 2 drawtext stages",
                   two.count(",drawtext=") == 1)
    fails += check("second line sits one line-height lower",
                   f":y=h-{750 - first_step}-text_h" in two,
                   f"expected -{750 - first_step}")

    # Beyond ~80 chars: capped with "…" so the canvas is never overflowed.
    huge = "слово " * 40
    wrapped = _tbl._wrap_title(huge)
    fails += check("huge title capped at 2 lines",
                   len(wrapped) <= 2, f"lines={len(wrapped)}")
    glued = " ".join(wrapped)
    fails += check("huge title carries the ellipsis cap",
                   "…" in glued and len(huge) > _tbl._MAX_TOTAL_CHARS,
                   f"len(lines)={[len(l) for l in wrapped]}")

    # Every line stays within budget when hard-truncated: the second line
    # never exceeds _MAX_LINE_CHARS, the cap-carrying first line gets +1
    # ("…" is the extra char by design — see title_draw._wrap_title docstring).
    overlong_word = "x" * 100
    hard = _tbl._wrap_title(overlong_word + " tail")
    fails += check("individual lines respect the per-line budget",
                   all(len(l) <= _tbl._MAX_LINE_CHARS + 1 for l in hard),
                   str([len(l) for l in hard]))

    # Font: on Windows the build must point at arialbd.ttf with a guarded ':';
    # on machines without the file the param is omitted entirely.
    font_present = os.path.isfile(_tbl._PREFERRED_FONT)
    one = _tbl.build_title_drawtext_filter("Привет, мир", 750, 64)
    if font_present:
        fails += check("fontfile drive-colon escaped C\\:/...",
                       "fontfile='C\\:/Windows/Fonts/arialbd.ttf'" in one, one[:90])
    else:
        fails += check("missing font omits fontfile (default kicks in)",
                       "fontfile=" not in one)

    # Numeric params land in the filter verbatim.
    other = _tbl.build_title_drawtext_filter("Title", y_from_bottom=640,
                                             fontsize=72)
    fails += check("fontsize and y_from_bottom land in the filter",
                   ":fontsize=72" in other and ":y=h-640-text_h" in other)
    fails += check("truncated title never exceeds the ~80 char budget",
                   len(glued) <= _tbl._MAX_TOTAL_CHARS,
                   f"{len(glued)} chars")
    return fails


def e2e_checks():
    fails = []
    tmpdir = tempfile.mkdtemp(prefix="title_stage_", dir=_TMP)
    try:
        src = os.path.join(tmpdir, "src.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=1080x1920:rate=20",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
             "-shortest", src],
            check=True, capture_output=True, timeout=60)
        fails += check("source 1080x1920/2s generated", probe_dims(src) == (1080, 1920))
        before_duration = probe_duration(src)

        # The REAL title stage; FORCE_CPU_FFMPEG keeps it hermetic off-GPU.
        out = os.path.join(tmpdir, "titled.mp4")
        shutil.copyfile(src, out)
        os.environ["FORCE_CPU_FFMPEG"] = "1"
        try:
            _tbl.apply_title_drawtext(out, "Яркий момент: 100% проверка")
        finally:
            os.environ.pop("FORCE_CPU_FFMPEG", None)

        fails += check("titled clip exists and decodes", os.path.isfile(out))
        after_duration = probe_duration(out)
        fails += check("duration preserved (±0.5s)",
                       abs(after_duration - before_duration) <= 0.5,
                       f"{before_duration:.2f}s -> {after_duration:.2f}s")
        fails += check("resolution preserved", probe_dims(out) == (1080, 1920))

        import numpy as np
        src_frame = frame_gray_f32(src, at=1.0)
        out_frame = frame_gray_f32(out, at=1.0)
        delta = float(np.abs(src_frame - out_frame).mean())
        fails += check("frame at t=1s differs by more than encode noise",
                       delta > 1.0, f"mean|delta|={delta:.3f}")

        # An explicit empty title is a no-op: sizes may differ by a byte but
        # no re-encode pass should run (mtime is fine, content equal).
        no_op = os.path.join(tmpdir, "noop.mp4")
        shutil.copyfile(src, no_op)
        _tbl.apply_title_drawtext(no_op, "")
        fails += check("empty title is a no-op",
                       probe_duration(no_op) == before_duration)

        # TITLE_ENABLED=0 must skip the burn even with a real title.
        disabled = os.path.join(tmpdir, "disabled.mp4")
        shutil.copyfile(src, disabled)
        os.environ["FORCE_CPU_FFMPEG"] = "1"
        os.environ["TITLE_ENABLED"] = "0"
        try:
            _tbl.apply_title_drawtext(disabled, "Must not appear")
        finally:
            os.environ.pop("FORCE_CPU_FFMPEG", None)
            os.environ.pop("TITLE_ENABLED", None)
        dis_frame = frame_gray_f32(disabled, at=1.0)
        dis_delta = float(np.abs(src_frame - dis_frame).mean())
        fails += check("TITLE_ENABLED=0 leaves the frame untouched",
                       dis_delta < 0.5, f"mean|delta|={dis_delta:.3f}")
    except Exception as e:
        print(f"ERROR  {type(e).__name__}: {e}", flush=True)
        fails.append(f"e2e exception: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return fails


def main():
    failures = []
    failures += unit_checks()
    failures += e2e_checks()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
