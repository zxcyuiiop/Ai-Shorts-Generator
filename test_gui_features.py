"""End-to-end checks for settings persistence, log streaming, and the timer."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import settings_store

# Use a scratch settings file so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.test.json"
)

import app as webapp

MASK = settings_store.MASK
failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def fake_pipeline(**kwargs):
    """Stand-in for generate_shorts that prints the markers the GUI parses."""
    print("[download/local] https://youtu.be/x @ 720p -> output/")
    print("[transcribe/local] faster-whisper model=base device=cpu")
    print("[transcribe/local] 42 segments, 610s of audio")
    print("[highlights] content=podcast density=high duration=610s")
    print("[pipeline/local] cropping 2 of 5 candidates")
    print("[clip/local] 1/2: First clip")
    time.sleep(0.2)
    return {
        "mode": kwargs.get("mode"),
        "source_video_url": "output/source_x.mp4",
        "transcript": {"duration": 610.0, "segments": []},
        "highlights": [{"title": "a"}, {"title": "b"}],
        "shorts": [{
            "title": "First clip", "start_time": 10.0, "end_time": 55.0, "score": 91,
            "hook_sentence": "Hook", "virality_reason": "Reason",
            "clip_url": os.path.abspath("output/short_01.mp4"),
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

    if os.path.exists(settings_store.SETTINGS_PATH):
        os.remove(settings_store.SETTINGS_PATH)

    # --- settings round-trip ---
    check("GET /api/settings empty at first", c.get("/api/settings").get_json() == {})

    c.post("/api/settings", json={
        "url": "https://youtu.be/abc", "mode": "local", "llm_provider": "nim",
        "nim_key": "nvapi-real-secret", "nim_model": "meta/llama-3.1-70b-instruct",
        "num_clips": 5,
    })
    got = c.get("/api/settings").get_json()
    check("plain settings persist", got.get("nim_model") == "meta/llama-3.1-70b-instruct", str(got.get("nim_model")))
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

    progresses = [s for s in stages]
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

    built = webapp._overrides_from("local", {}, whisper_device="cuda", whisper_model="small")
    check("GUI device reaches overrides", built.get("LOCAL_WHISPER_DEVICE") == "cuda", str(built))
    check("GUI model reaches overrides", built.get("LOCAL_WHISPER_MODEL") == "small", str(built))
    api_built = webapp._overrides_from("api", {"muapi": "k"}, whisper_device="cuda")
    check("whisper settings ignored in api mode", "LOCAL_WHISPER_DEVICE" not in api_built,
          str(api_built))

    # --- local file upload path ---
    import io as _io

    os.makedirs(webapp.UPLOAD_DIR, exist_ok=True)
    up = c.post("/api/upload", data={
        "video": (_io.BytesIO(b"\x00\x00\x00\x18ftypmp42fake"), "My Talk.mp4"),
    }, content_type="multipart/form-data")
    check("upload accepted", up.status_code == 200, str(up.status_code))
    uploaded = up.get_json() or {}
    check("upload returns a path", bool(uploaded.get("path")), str(uploaded))
    check("uploaded file exists on disk", os.path.exists(uploaded.get("path", "")))
    check("filename sanitised", " " not in os.path.basename(uploaded.get("path", "x")),
          os.path.basename(uploaded.get("path", "")))

    bad = c.post("/api/upload", data={
        "video": (_io.BytesIO(b"MZ"), "payload.exe"),
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

    if uploaded.get("path") and os.path.exists(uploaded["path"]):
        os.remove(uploaded["path"])

    if os.path.exists(settings_store.SETTINGS_PATH):
        os.remove(settings_store.SETTINGS_PATH)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
