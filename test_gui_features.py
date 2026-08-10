"""End-to-end checks for settings persistence, log streaming, and the timer.

Hermetic: the settings file and every dir the app can write to are redirected
into a fresh tempdir before app.py is imported, so a run can never touch the
real settings.local.json or the real output/ tree. The pipeline itself is
stubbed (no ffmpeg, whisper, LLM, or network). The tempdir is removed on exit.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="gui-features-")

from shorts_generator import settings_store  # noqa: E402

# Use a scratch settings file inside the tempdir so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

import app as webapp  # noqa: E402

# Send every write the app can do into the tempdir. app.py reads its module
# level LOCAL_OUTPUT_DIR everywhere, so patch that (not just UPLOAD_DIR).
webapp.LOCAL_OUTPUT_DIR = _TMP
webapp.UPLOAD_DIR = os.path.join(_TMP, "uploads")
webapp.MUSIC_UPLOAD_DIR = os.path.join(_TMP, "music")

MASK = settings_store.MASK
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def webapp_check_js(source):
    """True when node exists and accepts the script (``node --check``)."""
    import subprocess
    try:
        # Bytes on stdin: piping str lets the Windows console encoding
        # (cp1251) reject valid app.js characters like "×" in Russian UI text.
        proc = subprocess.run(["node", "--check", "-"], input=source.encode("utf-8"),
                              capture_output=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None  # node not installed -- nothing to assert on
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", "replace").strip()[:300])
    return proc.returncode == 0


def fake_pipeline(**kwargs):
    """Stand-in for generate_shorts that prints the markers the GUI parses.

    The local clip_url pretends the clip already lives inside the (temp) output
    dir, so the app's rewrite sees it as /output/short_01.mp4.
    """
    print("[download/local] https://youtu.be/x @ 720p -> output/")
    print("[transcribe/local] faster-whisper model=base device=cpu")
    print("[transcribe/local] 42 segments, 610s of audio")
    print("[highlights] content=podcast density=high duration=610s")
    print("[pipeline/local] cropping 2 of 5 candidates")
    print("[clip/local] 1/2: First clip")
    time.sleep(0.2)
    return {
        "mode": kwargs.get("mode"),
        "source_video_url": os.path.join(_TMP, "source_x.mp4"),
        "transcript": {"duration": 610.0, "segments": []},
        "highlights": [{"title": "a"}, {"title": "b"}],
        "shorts": [{
            "title": "First clip", "start_time": 10.0, "end_time": 55.0, "score": 91,
            "hook_sentence": "Hook", "virality_reason": "Reason",
            "clip_url": os.path.join(_TMP, "short_01.mp4"),
        }],
    }


def wait_done(client, job_id, tries=100):
    for _ in range(tries):
        job = client.get(f"/api/status/{job_id}").get_json()
        if job.get("status") in ("completed", "error"):
            return job
        time.sleep(0.1)
    return job


def main():
    webapp.generate_shorts = fake_pipeline
    c = webapp.app.test_client()

    try:
        # --- template/JS contract: every id app.js touches must exist once ---
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent

        check("index page renders", c.get("/").status_code == 200)
        r = c.get("/static/app.js")
        check("app.js served", r.status_code == 200, str(r.status_code))
        app_js = r.get_data(as_text=True)
        check("app.js parses under node", webapp_check_js(app_js) is not False)

        html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        html_ids = re.findall(r'id="([^"]+)"', html)
        dupes = sorted({i for i in html_ids if html_ids.count(i) > 1})
        check("no duplicate ids in template", not dupes, ", ".join(dupes))
        js_ids = sorted(set(re.findall(r"getElementById\('([^']+)'\)", app_js)))
        missing = [i for i in js_ids if i not in html_ids]
        # A missing id throws at the wiring block and takes every listener after
        # it down with it -- that is how the music upload button went dead.
        check("app.js ids exist in template", not missing, ", ".join(missing))
        check("music upload wiring present",
              "music_upload_btn" in js_ids and "/api/upload/music" in app_js)

        # --- wave 2f UX-hardening contract ---
        for new_id in ("queue-empty", "review-download-all-btn"):
            check(f"template has #{new_id}", new_id in html_ids)
        check("review region is labelled",
              'id="review-section" class="hidden" role="region" aria-label="Проверка шортов"' in html)
        check("review card is focusable",
              'class="review-card" tabindex="-1"' in html)
        check("progress bar is a progressbar", 'role="progressbar"' in html)
        check("toast close button wired", 'className = \'toast-close\'' in app_js)
        check("delete confirmation wired",
              'confirm(\'Удалить клип без возможности восстановить?\')' in app_js)
        check("queue duplicate confirm wired",
              'Запустить новую задачу вне очереди?' in app_js)

        # --- settings round-trip ---
        check("GET /api/settings empty at first", c.get("/api/settings").get_json() == {})

        c.post("/api/settings", json={
            "url": "https://youtu.be/abc", "mode": "local", "llm_provider": "nim",
            "nim_key": "nvapi-real-secret", "nim_model": "meta/llama-3.1-70b-instruct",
            "num_clips": 5,
        })
        got = c.get("/api/settings").get_json()
        check("plain settings persist", got.get("nim_model") == "meta/llama-3.1-70b-instruct",
              str(got.get("nim_model")))
        check("num_clips persists", got.get("num_clips") == 5, str(got.get("num_clips")))
        check("secret is masked to browser", got.get("nim_key") == MASK, str(got.get("nim_key")))
        check("secret stored in plaintext on disk",
              settings_store.load().get("nim_key") == "nvapi-real-secret")

        # Saving the mask back must not wipe the stored key.
        c.post("/api/settings", json={"nim_key": MASK, "num_clips": 7})
        check("re-saving mask keeps real key",
              settings_store.load().get("nim_key") == "nvapi-real-secret")
        check("other fields still update", settings_store.load().get("num_clips") == 7)

        # Unknown fields must not be written.
        c.post("/api/settings", json={"evil_field": "x"})
        check("unknown fields rejected", "evil_field" not in settings_store.load())

        check("mask resolves to stored secret",
              settings_store.resolve_secret("nim_key", MASK) == "nvapi-real-secret")
        check("real value passes through",
              settings_store.resolve_secret("nim_key", "nvapi-new") == "nvapi-new")

        # --- save-button contract: it must collect by id, not FormData ---
        # Баг «nim не сохраняется» жил не на сервере, а в JS: кнопка брала
        # FormData, а у полей провайдеров (id без name) его там не было.
        check("save button uses collectSettingsPayload()",
              "window.collectSettingsPayload()" in html)
        check("collector exists in app.js",
              "function collectSettingsPayload()" in app_js)
        check("collector exposed to the inline handler",
              "window.collectSettingsPayload = collectSettingsPayload" in app_js)
        check("collector iterates every persisted field",
              "for (const field of SETTING_FIELDS)" in app_js)
        nim_field = re.search(r'id="nim_key"[^>]*>', html)
        check("nim_key has no name (hence FormData never sent it)",
              nim_field is not None and "name=" not in nim_field.group(0))

        # Full round-trip exactly as the fixed front-end sends it: provider
        # fields present, empty secret absent (collector skips empty secrets so
        # a stored key is never clobbered by a blank box).
        c.post("/api/settings", json={
            "mode": "local", "llm_provider": "nim",
            "nim_key": "nvapi-round-trip", "nim_model": "meta/llama-3.1-8b-instruct",
            "captions_enabled": "1",
        })
        got = c.get("/api/settings").get_json()
        check("nim key+model persist through the new payload",
              settings_store.load().get("nim_key") == "nvapi-round-trip"
              and got.get("nim_key") == MASK
              and got.get("nim_model") == "meta/llama-3.1-8b-instruct")
        # A later save with the secret absent must keep it (matches collector:
        # empty secret fields are simply not sent).
        c.post("/api/settings", json={"mode": "local", "num_clips": "3",
                                      "captions_enabled": "0"})
        check("key survives a save that omits it",
              settings_store.load().get("nim_key") == "nvapi-round-trip")
        check("explicit checkbox off persists as '0'",
              settings_store.load().get("captions_enabled") == "0")

        # --- music upload: the GUI sends the audio under the "music" field ---
        wav = io.BytesIO(b"RIFF" + b"\x00" * 40)
        r = c.post("/api/upload/music", data={"music": (wav, "song.wav")},
                   content_type="multipart/form-data")
        body = r.get_json() or {}
        check("music upload ok", r.status_code == 200 and body.get("ok") is True,
              f"{r.status_code} {body}")
        check("music saved under music/",
              body.get("path", "").replace("\\", "/").endswith("music/" + body.get("filename", "\x00"))
              and body.get("filename", "").startswith("music_")
              and os.path.isfile(body.get("path", "")), str(body.get("path")))

        r = c.post("/api/upload/music", data={"music": (io.BytesIO(b"x"), "song.exe")},
                   content_type="multipart/form-data")
        check("music upload rejects bad extension", r.status_code == 400)

        r = c.post("/api/upload/music", data={}, content_type="multipart/form-data")
        check("music upload rejects missing file", r.status_code == 400)

        # --- generation: log streaming + timer ---
        r = c.post("/api/generate", json={
            "url": "https://youtu.be/x", "mode": "local", "llm_provider": "ollama",
            "num_clips": 1, "api_keys": {"ollama_model": "qwen2.5:14b"},
        })
        job_id = r.get_json()["job_id"]

        seen_lines, stages, elapsed_seen, final = [], [], [], None
        resp = c.get(f"/api/progress/{job_id}")
        for raw in resp.response:
            text = raw.decode()
            if not text.startswith("data: "):
                continue
            evt = json.loads(text[6:])
            if evt.get("line"):
                seen_lines.append(evt["line"])
            if evt.get("stage"):
                stages.append(evt["stage"])
            if isinstance(evt.get("elapsed"), (int, float)):
                elapsed_seen.append(evt["elapsed"])
            if evt.get("stage") == "done" or evt.get("status") == "error":
                final = evt
                break

        check("real pipeline lines streamed", any("faster-whisper" in l for l in seen_lines),
              f"{len(seen_lines)} lines")
        check("download stage detected", "downloading" in stages)
        check("transcribing stage detected", "transcribing" in stages)
        check("analyzing stage detected", "analyzing" in stages)
        check("rendering stage detected", "rendering" in stages)
        check("reaches done", final is not None and final.get("stage") == "done")
        check("elapsed present and non-negative", bool(elapsed_seen) and min(elapsed_seen) >= 0)
        check("elapsed is monotonic", elapsed_seen == sorted(elapsed_seen))

        job = wait_done(c, job_id)
        check("job completed", job.get("status") == "completed", str(job.get("status")))
        check("final elapsed recorded", isinstance(job.get("elapsed"), (int, float)))
        check("local path rewritten for browser",
              job["result"]["shorts"][0]["clip_url"] == "/output/short_01.mp4",
              job["result"]["shorts"][0]["clip_url"])
        check("log retained on job", len(job.get("log", [])) >= 6, str(len(job.get("log", []))))

        # --- generate also persists settings ---
        saved = settings_store.load()
        check("generate persisted ollama_model", saved.get("ollama_model") == "qwen2.5:14b",
              str(saved.get("ollama_model")))
        check("generate persisted mode", saved.get("mode") == "local")

        # --- progress never goes backwards across chunked runs ---
        def chunky(**kwargs):
            print("[highlights] chunk 1/3 (offset 0s)")
            print("[clip/local] 1/2: x")
            print("[highlights] chunk 2/3 (offset 1140s)")  # would regress to 60
            return {"mode": "local", "source_video_url": "s", "transcript": {},
                    "highlights": [], "shorts": []}

        webapp.generate_shorts = chunky
        r = c.post("/api/generate", json={"url": "https://youtu.be/y", "mode": "local"})
        jid = r.get_json()["job_id"]
        seq = []
        resp = c.get(f"/api/progress/{jid}")
        for raw in resp.response:
            text = raw.decode()
            if not text.startswith("data: "):
                continue
            evt = json.loads(text[6:])
            if isinstance(evt.get("progress"), (int, float)):
                seq.append(evt["progress"])
            if evt.get("stage") == "done" or evt.get("status") == "error":
                break
        check("progress never decreases", seq == sorted(seq), str(seq))

        # --- error path ---
        def boom(**kwargs):
            print("[download/local] starting")
            raise RuntimeError("could not find codec parameters")

        webapp.generate_shorts = boom
        r = c.post("/api/generate", json={"url": "https://youtu.be/z", "mode": "local"})
        job = wait_done(c, r.get_json()["job_id"])
        check("pipeline error surfaces", job.get("status") == "error", str(job.get("status")))
        check("error message preserved", "codec parameters" in (job.get("error") or ""))
        check("elapsed recorded on failure", isinstance(job.get("elapsed"), (int, float)))

        # --- whisper device / model overrides reach the transcriber ---
        from shorts_generator import config as cfg
        from shorts_generator.local import transcriber as tr

        cfg.set_overrides({"LOCAL_WHISPER_DEVICE": "cuda"})
        check("explicit cuda is honoured", tr._resolve_device() == "cuda", tr._resolve_device())
        cfg.set_overrides({"LOCAL_WHISPER_DEVICE": "cpu"})
        check("explicit cpu is honoured", tr._resolve_device() == "cpu", tr._resolve_device())
        cfg.set_overrides({"LOCAL_WHISPER_DEVICE": "auto"})
        check("auto resolves to a real device", tr._resolve_device() in ("cpu", "cuda"),
              tr._resolve_device())
        cfg.set_overrides({"LOCAL_WHISPER_MODEL": "medium"})
        check("whisper model override applies", cfg.env("LOCAL_WHISPER_MODEL", "base") == "medium")
        cfg.clear_overrides()
        check("override cleared", cfg.env("LOCAL_WHISPER_MODEL", "base") == "base")

        # _overrides_from is positional in this branch; whisper goes 3rd/4th.
        built = webapp._overrides_from("local", {}, "cuda", "small")
        check("GUI device reaches overrides", built.get("LOCAL_WHISPER_DEVICE") == "cuda", str(built))
        check("GUI model reaches overrides", built.get("LOCAL_WHISPER_MODEL") == "small", str(built))
        api_built = webapp._overrides_from("api", {"muapi": "k"}, "cuda", "base")
        check("whisper settings ignored in api mode", "LOCAL_WHISPER_DEVICE" not in api_built,
              str(api_built))

        # --- local file upload path ---
        up = c.post("/api/upload", data={
            "video": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42fake"), "My Talk.mp4"),
        }, content_type="multipart/form-data")
        check("upload accepted", up.status_code == 200, str(up.status_code))
        uploaded = up.get_json() or {}
        check("upload returns a path", bool(uploaded.get("path")), str(uploaded))
        check("uploaded file exists on disk", os.path.exists(uploaded.get("path", "")))
        check("uploaded file stays inside the tempdir",
              os.path.realpath(uploaded.get("path", "")).startswith(os.path.realpath(_TMP)),
              uploaded.get("path", "MISSING"))
        check("filename sanitised", " " not in os.path.basename(uploaded.get("path", "x")),
              os.path.basename(uploaded.get("path", "")))

        bad = c.post("/api/upload", data={
            "video": (io.BytesIO(b"MZ"), "payload.exe"),
        }, content_type="multipart/form-data")
        check("non-video extension rejected", bad.status_code == 400, str(bad.status_code))

        empty = c.post("/api/upload", data={}, content_type="multipart/form-data")
        check("missing file rejected", empty.status_code == 400, str(empty.status_code))

        # A file source with api mode is a contradiction -- must be refused.
        webapp.generate_shorts = fake_pipeline
        clash = c.post("/api/generate", json={
            "url": uploaded.get("path", "x.mp4"), "source_type": "file", "mode": "api",
        })
        check("file + api mode refused", clash.status_code == 400, str(clash.status_code))

        ok = c.post("/api/generate", json={
            "url": uploaded.get("path", "x.mp4"), "source_type": "file", "mode": "local",
            "num_clips": 1, "whisper_device": "cpu", "whisper_model": "tiny",
        })
        check("file + local mode accepted", ok.status_code == 202, str(ok.status_code))
        fjob = wait_done(c, ok.get_json()["job_id"])
        check("upload job completes", fjob.get("status") == "completed", str(fjob.get("status")))

        saved = settings_store.load()
        check("whisper device persisted", saved.get("whisper_device") == "cpu", str(saved.get("whisper_device")))
        check("whisper model persisted", saved.get("whisper_model") == "tiny", str(saved.get("whisper_model")))
        check("uploaded path not saved as url", not saved.get("url"), str(saved.get("url")))
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
