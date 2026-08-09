"""Checks for the watermark / blur-bars toggle surviving into /api/shorts/finalize.

Two fixes are exercised together:
  1) settings_store.save persists lowercase GUI keys (`overlay_enabled: false`)
     as uppercase config aliases (`OVERLAY_ENABLED: "0"`) so config.env can see
     them without a per-request set_overrides thread-local.
  2) finalize_short wraps `finalize_clip_local` in set_overrides(...) /
     clear_overrides() built from the producing job's stored params, so the
     browser's toggle applies at finalize time even though it runs in a fresh
     thread.

Everything is stubbed: no network, no ffmpeg, no real rendering.
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import settings_store  # noqa: E402

# Use a scratch settings file so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(
    tempfile.mkdtemp(prefix="watermark-toggle-"), "settings.test.json"
)

# Point the output-serve logic at a scratch dir so a draft created here can be
# resolved by _url_to_output_path.
OUTPUT_DIR = tempfile.mkdtemp(prefix="watermark-toggle-out-")

from shorts_generator import config as cfg  # noqa: E402
import app as webapp  # noqa: E402

webapp.LOCAL_OUTPUT_DIR = OUTPUT_DIR

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def _reset():
    if os.path.exists(settings_store.SETTINGS_PATH):
        os.remove(settings_store.SETTINGS_PATH)
    cfg.clear_overrides()


def run_settings_alias_checks():
    _reset()

    # 1. Saving a lowercase bool produces an uppercase string visible via
    # config.env, even after clear_overrides (no thread-local on this thread).
    settings_store.save({"overlay_enabled": False})
    check("settings alias: OVERLAY_ENABLED persisted on save",
          os.path.exists(settings_store.SETTINGS_PATH))
    val = cfg.env("OVERLAY_ENABLED", "1")
    check("config.env reads OVERLAY_ENABLED='0' after clear_overrides",
          val == "0", f"got {val!r}")

    # 2. blur_bars -> BLUR_BARS is also bridged.
    settings_store.save({"blur_bars": False})
    val = cfg.env("BLUR_BARS", "1")
    check("config.env reads BLUR_BARS='0' after clear_overrides",
          val == "0", f"got {val!r}")

    # 3. music toggles too (music_file is a free string, not normalized).
    settings_store.save({"music_enabled": True, "music_file": "out.mp3",
                         "music_volume": 33})
    check("config.env reads MUSIC_ENABLED='1'", cfg.env("MUSIC_ENABLED", "0") == "1")
    check("config.env reads MUSIC_FILE", cfg.env("MUSIC_FILE", "") == "out.mp3")
    check("config.env reads MUSIC_VOLUME='33'",
          cfg.env("MUSIC_VOLUME", "40") == "33",
          f"got {cfg.env('MUSIC_VOLUME', '40')!r}")

    # 4. silence_cut False -> SILENCE_CUT='0'.
    settings_store.save({"silence_cut": False})
    check("config.env reads SILENCE_CUT='0'",
          cfg.env("SILENCE_CUT", "1") == "0",
          f"got {cfg.env('SILENCE_CUT', '1')!r}")

    # 5. Partial saves don't clobber existing aliases.
    settings_store.save({"overlay_enabled": True, "blur_bars": True})
    check("re-save: overlay_enabled=True -> '1'",
          cfg.env("OVERLAY_ENABLED", "") == "1",
          f"got {cfg.env('OVERLAY_ENABLED', '')!r}")
    check("re-save: blur_bars=True -> '1'",
          cfg.env("BLUR_BARS", "") == "1",
          f"got {cfg.env('BLUR_BARS', '')!r}")


def run_finalize_endpoint_checks():
    _reset()
    c = webapp.app.test_client()

    # A draft under the scratch output dir so _url_to_output_path accepts it.
    draft_rel = "fakejob/draft_01.mp4"
    draft_abs = os.path.realpath(os.path.join(OUTPUT_DIR, draft_rel))
    os.makedirs(os.path.dirname(draft_abs), exist_ok=True)
    with open(draft_abs, "wb") as f:
        f.write(b"draftbytes")

    # Register a job that produced this clip. Params are what the generation
    # thread captured from the browser form.
    job_id = "job_watermark_toggle"
    jobs_dict = webapp.jobs
    jobs_dict[job_id] = {
        "status": "completed",
        "aspect_ratio": "9:16",
        "url": "https://example.com/v",
        "added_at": 0.0,
        "started_at": 0.0,
        "finished_at": 1.0,
        "log": [],
        "result": {"shorts": [{"clip_url": f"/output/{draft_rel}"}]},
        "aspect_ratio": "9:16",
        "mode": "local",
        "llm_provider": "openai",
        "_params": {
            "mode": "local",
            "llm_provider": "openai",
            "api_keys": {},
            "whisper_device": "auto",
            "whisper_model": "base",
            "clip_length": "any",
            "overlay_position": "br",
            "overlay_margin": "24",
            "overlay_scale": "1.0",
            "use_overlay_opencv": "1",
            "overlay_vertical_pos": None,
            "overlay_margin_bottom": None,
            "overlay_margin_left": None,
            "overlay_enabled": False,   # user unchecked the watermark
            "overlay_x": None,
            "overlay_y": None,
            "music_enabled": False,
            "music_file": None,
            "music_volume": 40,
            "silence_cut": True,
            "blur_bars": False,
        },
    }

    # Capture which env the finalize actually sees: the stub asserts the
    # watermark/blur toggles are "0" inside the call. Anything else (defaults
    # lookup, missing overrides) raises so the check would fail.
    from shorts_generator.local import clipper as clip
    seen = {}

    def stubbed(path, aspect):
        seen["OVERLAY_ENABLED"] = cfg.env("OVERLAY_ENABLED", "1")
        seen["BLUR_BARS"] = cfg.env("BLUR_BARS", "1")
        seen["SILENCE_CUT"] = cfg.env("SILENCE_CUT", "1")
        # Stub must "complete" the file so the endpoint thinks it worked.
        with open(path, "wb") as f:
            f.write(b"finalized")

    real = clip.finalize_clip_local
    try:
        clip.finalize_clip_local = stubbed
        # Also patch the imported reference inside app.finalize_short.
        # It's imported lazily in the function body, so the same stub is used.
        r = c.post("/api/shorts/finalize", json={"url": f"/output/{draft_rel}"})
    finally:
        clip.finalize_clip_local = real

    body = r.get_json() or {}
    check("finalize: 200 ok", r.status_code == 200 and body.get("ok") is True,
          f"status={r.status_code} body={body}")
    check("finalize: stub saw OVERLAY_ENABLED='0'",
          seen.get("OVERLAY_ENABLED") == "0",
          f"seen={seen}")
    check("finalize: stub saw BLUR_BARS='0'",
          seen.get("BLUR_BARS") == "0",
          f"seen={seen}")
    check("finalize: stub saw SILENCE_CUT='1' (user left it on)",
          seen.get("SILENCE_CUT") == "1",
          f"seen={seen}")

    # Same scenario but explicitly toggle watermarks ON: the override must
    # override the persisted settings too (so a refresh of the page doesn't
    # silently disable the user's intent). Only the job params are consulted.
    settings_store.save({"overlay_enabled": True, "blur_bars": True})
    jobs_dict[job_id]["_params"]["overlay_enabled"] = False
    jobs_dict[job_id]["_params"]["blur_bars"] = False

    seen.clear()
    try:
        clip.finalize_clip_local = stubbed
        r = c.post("/api/shorts/finalize", json={"url": f"/output/{draft_rel}",
                                                 "aspect_ratio": "9:16"})
    finally:
        clip.finalize_clip_local = real

    check("finalize: 200 ok (after settings resave)",
          r.status_code == 200 and (r.get_json() or {}).get("ok") is True,
          f"status={r.status_code}")
    check("finalize: job _params still override persisted settings",
          seen.get("OVERLAY_ENABLED") == "0" and seen.get("BLUR_BARS") == "0",
          f"seen={seen}")

    # Clean up the job so the module-level jobs dict doesn't leak into other
    # test suites importing app.
    jobs_dict.pop(job_id, None)


def main():
    try:
        run_settings_alias_checks()
        run_finalize_endpoint_checks()
    finally:
        _reset()
        for path in (settings_store.SETTINGS_PATH, OUTPUT_DIR):
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else None
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
