"""E2E test for the two-stage local render (draft -> finalize).

Uses REAL ffmpeg against a tiny generated source clip:
  1. crop_clip_local(..., finalize=False) must produce ONLY a reframed draft
     (no blur-pad to 1080x1920, no TikTok overlay, no music) and print the
     deferred log line.
  2. finalize_clip_local(draft, "9:16") must then apply the effects in place:
     blur-pad (BLUR_BARS=1), TikTok overlay, and the music bed (MUSIC_ENABLED=1).

Hermetic: the heavy per-stage network/disk surprises are stubbed -- the
clipper's local ``_overlay_tiktok`` (OpenCV loop), ``apply_blur_padding``
(its own ffmpeg call), and ``mix_music`` (its ffmpeg call) are replaced with
Recording stubs so no frame/overlay/music heavy work runs. Real ffmpeg still
runs for the source generation, the draft reframe, and every ffprobe check
(the steps actually under test). If ffmpeg/ffprobe are not installed this test
prints SKIP and exits 0.
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

# Gate: without ffmpeg/ffprobe there is nothing to E2E. Print SKIP and exit.
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not installed; two-stage E2E skipped")
    sys.exit(0)

_TMP = tempfile.mkdtemp(prefix="finalize-e2e-")

# Neutralize the settings layer BEFORE importing anything that reads it.
from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

# Import module objects (not from-imports) so the stubs below affect the live code.
from shorts_generator.local import clipper as _clip  # noqa: E402

captured_crop = _clip.crop_clip_local
captured_finalize = _clip.finalize_clip_local

OVERLAY_PATH = os.path.join(_HERE, "TIKTOK1.mov")


# --- helpers -----------------------------------------------------------------
@contextlib.contextmanager
def step_env(**overrides):
    """Deterministic env for one pipeline call: only what's passed survives."""
    saved = dict(os.environ)
    try:
        for k in ("SILENCE_CUT", "BLUR_BARS", "OVERLAY_ENABLED", "MUSIC_ENABLED",
                  "MUSIC_FILE", "MUSIC_VOLUME", "FORCE_CPU_FFMPEG", "FFMPEG_ENCODER"):
            os.environ.pop(k, None)
        os.environ.update({k: str(v) for k, v in overrides.items()})
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def captured(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def log_tail(text, n=6):
    lines = (text or "").strip().splitlines()
    return " | ".join(lines[-n:]) if lines else "(no output captured)"


def run(cmd, what, timeout=120):
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
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}",
              flush=True)
        if not cond:
            failures.append(name)

    # Record the heavy stages instead of running them; the rest stays real.
    calls = {"blur": [], "overlay": [], "music": []}
    real_blur = _clip.apply_blur_padding
    real_overlay = _clip._overlay_tiktok
    real_music = _clip.mix_music

    def blur_stub(in_path, out_path):
        calls["blur"].append((in_path, out_path))
        shutil.copyfile(in_path, out_path)
        return out_path

    def overlay_stub(base, overlay_path):
        calls["overlay"].append((base, overlay_path))

    def music_stub(out_path, music_file, volume):
        calls["music"].append((out_path, music_file, volume))
        return out_path

    _clip.apply_blur_padding = blur_stub
    _clip._overlay_tiktok = overlay_stub
    _clip.mix_music = music_stub

    tmpdir = tempfile.mkdtemp(prefix="finalize_stage_", dir=_TMP)
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

        # 2. DRAFT: finalize=False must skip every effect --------------------
        draft = os.path.join(tmpdir, "draft.mp4")
        with step_env(SILENCE_CUT="0", FORCE_CPU_FFMPEG="1"):
            draft_result, draft_log = captured(captured_crop, src, 0.0, 2.5, "9:16", draft, finalize=False)
        check("draft render returns the requested path", draft_result == draft)
        check("draft file exists and decodes", os.path.isfile(draft) and probe_ok(draft))
        dw, dh = probe_dims(draft)
        check("draft reframe is vertical (height >= width)", dh >= dw, f"{dw}x{dh}")
        check("draft is NOT 1080x1920 (blur-pad correctly deferred)",
              (dw, dh) != (1080, 1920), f"{dw}x{dh}")
        check("draft printed 'finalize deferred (draft)'",
              "finalize deferred (draft)" in draft_log, log_tail(draft_log))
        check("no heavy stage ran for the draft",
              not calls["blur"] and not calls["overlay"] and not calls["music"],
              f"blur={len(calls['blur'])} overlay={len(calls['overlay'])} music={len(calls['music'])}")

        # 3. FINALIZE on the draft: blur-pad + overlay (+ music off) ---------
        calls["blur"].clear(); calls["overlay"].clear(); calls["music"].clear()
        with step_env(OVERLAY_ENABLED="1", BLUR_BARS="1", MUSIC_ENABLED="0",
                      FORCE_CPU_FFMPEG="1"):
            fin_result, fin_log = captured(captured_finalize, draft, "9:16")
        check("finalize returns the same path", fin_result == draft)
        check("finalized clip still decodes", os.path.isfile(draft) and probe_ok(draft))
        check("blur-pad stage ran during finalize", bool(calls["blur"]), f"calls={calls['blur']}")
        # TIKTOK1.mov is a large gitignored asset that only exists in a real
        # checkout; on a fresh clone the clipper must skip the stage gracefully.
        if os.path.isfile(OVERLAY_PATH):
            check("TikTok overlay stage ran during finalize", bool(calls["overlay"]),
                  f"calls={len(calls['overlay'])}")
        else:
            check("missing TIKTOK1.mov logged and skipped gracefully",
                  "TikTok overlay file not found" in fin_log and not calls["overlay"],
                  log_tail(fin_log, 4))
        check("music stayed off during finalize (MUSIC_ENABLED=0)",
              not calls["music"] and "music: mixing" not in fin_log, log_tail(fin_log))

        # 4. FINALIZE with the music bed enabled -----------------------------
        music_wav = os.path.join(tmpdir, "music.wav")
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
             "-af", "volume=0.5", music_wav], "generate tiny music wav")
        draft2 = os.path.join(tmpdir, "draft_music.mp4")
        with step_env(SILENCE_CUT="0", FORCE_CPU_FFMPEG="1"):
            captured(captured_crop, src, 0.0, 2.5, "9:16", draft2, finalize=False)
        check("second draft exists for the music run", os.path.isfile(draft2) and probe_ok(draft2))
        calls["blur"].clear(); calls["overlay"].clear(); calls["music"].clear()
        with step_env(OVERLAY_ENABLED="0", BLUR_BARS="0", MUSIC_ENABLED="1",
                      MUSIC_FILE=music_wav, MUSIC_VOLUME="40", FORCE_CPU_FFMPEG="1"):
            _, mus_log = captured(captured_finalize, draft2, "9:16")
        check("music mixer ran during the enabled run", bool(calls["music"]), f"calls={calls['music']}")
        check("music-mixed clip still decodes", probe_ok(draft2))
        check("no blur-pad when BLUR_BARS=0", not calls["blur"], f"calls={calls['blur']}")
    except Exception as e:
        print(f"ERROR  {type(e).__name__}: {e}")
        return 1
    finally:
        # Restore the real implementations no matter what.
        _clip.apply_blur_padding = real_blur
        _clip._overlay_tiktok = real_overlay
        _clip.mix_music = real_music
        shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
