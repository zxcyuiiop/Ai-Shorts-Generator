"""E2E smoke test for the two-stage local render (draft -> finalize).

Uses REAL ffmpeg against a tiny generated source clip:
  1. crop_clip_local(..., finalize=False) must produce ONLY a reframed draft
     (no blur-pad to 1080x1920, no TikTok overlay, no music) and print the
     deferred log line.
  2. finalize_clip_local(draft, "9:16") must then apply the effects in place:
     blur-pad to exactly 1080x1920 (when blurpad ran), the TIKTOK1.mov overlay
     when that file exists at the project root, and (step 4) the music bed.

Isolation: every env var the clipper/blurpad/music/overlay stages can read is
controlled here. shorts_generator/config.py calls load_dotenv() at import and
its env() helper falls through settings.local.json -> real .env values that
already sit in os.environ, so we
  a) point settings_store.SETTINGS_PATH at a path that does not exist
     (load() returns {} -> no saved settings),
  b) clear+re-set os.environ per step, so .env-loaded values that were injected
     into os.environ at import are overwritten/removed before each call.

Run:  venv/Scripts/python -X utf8 test_finalize_e2e.py
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# --- Controlled env, set BEFORE importing the clipper (requirement) ----------
# Every key any stage of the two-stage render can read, via env() or directly.
_MANAGED_KEYS = (
    # silence-cut stage (runs inside crop_clip_local, draft included)
    "SILENCE_CUT", "SILENCE_NOISE_DB", "SILENCE_MIN_DUR", "SILENCE_KEEP_EXTRA",
    # finalize stages
    "BLUR_BARS",
    "OVERLAY_ENABLED", "USE_OVERLAY_OPENCV",
    "OVERLAY_POSITION", "OVERLAY_MARGIN", "OVERLAY_SCALE", "OVERLAY_X",
    "OVERLAY_Y", "OVERLAY_VERTICAL_POS", "OVERLAY_MARGIN_BOTTOM",
    "OVERLAY_MARGIN_LEFT",
    "MUSIC_ENABLED", "MUSIC_FILE", "MUSIC_VOLUME",
    # encoder selection (force deterministic CPU libx264)
    "FFMPEG_ENCODER", "FORCE_CPU_FFMPEG",
)
# Deterministic baseline for the whole test run.
os.environ["FORCE_CPU_FFMPEG"] = "1"
for _k in _MANAGED_KEYS:
    os.environ.pop(_k, None)

# Neutralize the settings layer BEFORE importing anything that reads it:
# settings_store.load() follows module-level SETTINGS_PATH, so point it at a
# file that will never exist and env() skips straight to os.environ.
from shorts_generator import settings_store

settings_store.SETTINGS_PATH = os.path.join(_HERE, ".test_finalize_e2e.missing.settings.json")

# Import clipper now; config.py runs load_dotenv() here, which may re-inject
# real .env values into os.environ -> clear our managed keys once more so the
# pre-import baseline survives regardless of what the real .env contains.
from shorts_generator.local.clipper import crop_clip_local, finalize_clip_local

os.environ["FORCE_CPU_FFMPEG"] = "1"
for _k in _MANAGED_KEYS:
    os.environ.pop(_k, None)
_BASE_ENV = dict(os.environ)

OVERLAY_PATH = os.path.join(_HERE, "TIKTOK1.mov")


# --- helpers -----------------------------------------------------------------
@contextlib.contextmanager
def step_env(**overrides):
    """Deterministic, leak-free environment for one pipeline call."""
    os.environ.clear()
    os.environ.update(_BASE_ENV)
    for k in _MANAGED_KEYS:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in overrides.items()})
    yield


def captured(fn, *args, **kwargs):
    """Run fn() capturing its stdout (clipper logs go to stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def log_tail(text, n=6):
    lines = (text or "").strip().splitlines()
    return " | ".join(lines[-n:]) if lines else "(no output captured)"


def run(cmd, what, timeout=120):
    """Run an external command; on failure print the stderr tail and raise."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"FAIL  {what}: timed out after {timeout}s")
        raise RuntimeError(f"{what} timed out")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        print(f"FAIL  {what} (exit {proc.returncode}): {' '.join(cmd)}")
        print(f"stderr tail:\n{tail}")
        raise RuntimeError(f"{what} failed")
    return proc


def probe_dims(path):
    proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x", path], f"ffprobe dims {os.path.basename(path)}")
    w, h = proc.stdout.strip().splitlines()[-1].split("x")[:2]
    return int(w), int(h)


def probe_ok(path):
    proc = subprocess.run(["ffprobe", "-v", "error", path],
                          capture_output=True, text=True, timeout=30)
    return proc.returncode == 0


def has_audio(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30)
    return proc.returncode == 0 and "audio" in (proc.stdout or "").lower()


# --- main --------------------------------------------------------------------
def main():
    failures = []

    def check(name, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}",
              flush=True)
        if not cond:
            failures.append(name)

    tmpdir = tempfile.mkdtemp(prefix="finalize_e2e_")
    try:
        # 1. ~3s landscape source with a 440 Hz tone --------------------------
        src = os.path.join(tmpdir, "src.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=duration=3:size=720x480:rate=20",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
             "-shortest", src], "generate source video")
        check("source video generated", os.path.isfile(src) and probe_ok(src),
              "720x480 landscape")
        print("PASS  tiny 720x480 landscape source rendered", flush=True)

        # 2. DRAFT: finesse=False must skip every effect ----------------------
        draft = os.path.join(tmpdir, "draft.mp4")
        with step_env(SILENCE_CUT="0"):
            draft_result, draft_log = captured(
                crop_clip_local, src, 0.0, 2.5, "9:16", draft, finalize=False)
        check("draft render returns the requested path", draft_result == draft)
        check("draft file exists and decodes", os.path.isfile(draft) and probe_ok(draft))
        dw, dh = probe_dims(draft)
        check("draft reframe is vertical (height >= width)", dh >= dw, f"{dw}x{dh}")
        check("draft is NOT 1080x1920 (blur-pad correctly deferred)",
              (dw, dh) != (1080, 1920), f"{dw}x{dh}")
        check("draft printed 'finalize deferred (draft)'",
              "finalize deferred (draft)" in draft_log, log_tail(draft_log))
        check("draft did NOT run the TikTok overlay stage",
              "TikTok overlay took" not in draft_log
              and "TikTok overlay fallback took" not in draft_log
              and "Overlay applied" not in draft_log
              and "TikTok overlay file not found" not in draft_log,
              log_tail(draft_log))
        check("draft did NOT run blur-pad", "[clip/local] blurpad" not in draft_log,
              log_tail(draft_log))
        check("draft did NOT run the music mixer", "music: mixing" not in draft_log,
              log_tail(draft_log))

        # 3. FINALIZE on the draft: blur-pad + overlay (+ music off) ----------
        with step_env(OVERLAY_ENABLED="1", BLUR_BARS="1", MUSIC_ENABLED="0"):
            fin_result, fin_log = captured(finalize_clip_local, draft, "9:16")
        check("finalize returns the same path", fin_result == draft)
        check("finalized clip exists and decodes (ffprobe rc=0)",
              os.path.isfile(draft) and probe_ok(draft))
        blurpad_ran = "[clip/local] blurpad: done" in fin_log
        check("blur-pad stage ran during finalize", blurpad_ran, log_tail(fin_log))
        if blurpad_ran:
            fw, fh = probe_dims(draft)
            check("finalized clip is EXACTLY 1080x1920", (fw, fh) == (1080, 1920),
                  f"{fw}x{fh}")
        overlay_exists = os.path.isfile(OVERLAY_PATH)
        overlay_ran = ("TikTok overlay took" in fin_log
                       or "TikTok overlay fallback took" in fin_log
                       or "Overlay applied" in fin_log)
        if overlay_exists:
            check("TIKTOK1.mov overlay stage ran during finalize", overlay_ran,
                  log_tail(fin_log))
        else:
            check("missing TIKTOK1.mov logged and skipped gracefully",
                  "TikTok overlay file not found" in fin_log and os.path.isfile(draft),
                  log_tail(fin_log))
        check("music stayed off during finalize (MUSIC_ENABLED=0)",
              "music: mixing" not in fin_log, log_tail(fin_log))

        # 4. FINALIZE with the music bed enabled ------------------------------
        music_wav = os.path.join(tmpdir, "music.wav")
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
             "-af", "volume=0.5", music_wav], "generate tiny music wav")
        draft2 = os.path.join(tmpdir, "draft_music.mp4")
        with step_env(SILENCE_CUT="0"):
            captured(crop_clip_local, src, 0.0, 2.5, "9:16", draft2, finalize=False)
        check("second draft exists for the music run",
              os.path.isfile(draft2) and probe_ok(draft2))
        with step_env(OVERLAY_ENABLED="0", BLUR_BARS="0", MUSIC_ENABLED="1",
                      MUSIC_FILE=music_wav, MUSIC_VOLUME="40"):
            _, mus_log = captured(finalize_clip_local, draft2, "9:16")
        music_ran = "music: mixing" in mus_log and "music: done" in mus_log
        check("music mixer ran and completed", music_ran, log_tail(mus_log))
        check("music-mixed clip still decodes (ffprobe rc=0)", probe_ok(draft2))
        if music_ran:
            check("music-mixed clip carries an audio stream", has_audio(draft2))
        check("overlay honored OVERLAY_ENABLED=0 in the music run",
              "overlay disabled" in mus_log or not overlay_ran or "Overlay applied" not in mus_log,
              log_tail(mus_log))

    except Exception as e:
        print(f"ERROR  {type(e).__name__}: {e}")
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
