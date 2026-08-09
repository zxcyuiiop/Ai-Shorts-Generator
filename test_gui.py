"""Smoke test for the web layer: drives every endpoint with a stubbed pipeline.

Hermetic: the settings file and every dir the app can write to are redirected
into a fresh tempdir before app.py is imported, so a run can never touch the
real settings.local.json or the real output/ tree. The pipeline itself is
stubbed (no ffmpeg, whisper, LLM, or network). The tempdir is removed on exit.
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="gui-test-")

from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

import app as webapp  # noqa: E402

# Send every write the app can do into the tempdir. app.py reads its module
# level LOCAL_OUTPUT_DIR everywhere, so patch that (not just UPLOAD_DIR).
webapp.LOCAL_OUTPUT_DIR = _TMP
webapp.UPLOAD_DIR = os.path.join(_TMP, "uploads")
webapp.MUSIC_UPLOAD_DIR = os.path.join(_TMP, "music")


def fake_generate_shorts(**kwargs):
    time.sleep(0.2)
    mode = kwargs.get("mode")
    # Print the markers the GUI parses into stages, so the SSE check can see
    # each stage transition instead of only starting/done. The local clip_url
    # pretends the clip already lives inside the (temp) output dir.
    print("[download] https://youtu.be/x @ 720p -> output/")
    print("[transcribe] faster-whisper model=base device=cpu")
    print("[highlights] content=podcast density=high duration=120s")
    print("[pipeline/local] cropping 1 of 2 candidates")
    print("[clip/local] 1/2: Clip One")
    return {
        "mode": mode,
        "source_video_url": "https://example.com/src.mp4",
        "transcript": {"duration": 120.0, "segments": [{"start": 0, "end": 5, "text": "hi"}]},
        "highlights": [{"title": "A"}, {"title": "B"}],
        "shorts": [
            {
                "title": "Clip One", "start_time": 10.0, "end_time": 55.5, "score": 92,
                "hook_sentence": "Nobody talks about this", "virality_reason": "Curiosity gap",
                "clip_url": (os.path.join(_TMP, "short_01.mp4") if mode == "local"
                             else "https://cdn.example.com/short_1.mp4"),
            },
            {
                "title": "Clip Two", "start_time": 70.0, "end_time": 110.0, "score": 81,
                "hook_sentence": "I was wrong", "virality_reason": "Reversal",
                "clip_url": None, "error": "render failed",
            },
        ],
    }


def wait_for_finish(client, job_id, tries=80):
    for _ in range(tries):
        job = client.get(f"/api/status/{job_id}").get_json()
        if job.get("status") in ("completed", "error"):
            return job
        time.sleep(0.1)
    return job


def main():
    webapp.generate_shorts = fake_generate_shorts
    c = webapp.app.test_client()
    failures = []

    def check(name, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    try:
        r = c.get("/")
        check("GET / renders form", r.status_code == 200 and b'id="generate-form"' in r.data,
              f"status={r.status_code}")

        r = c.post("/api/generate", json={"url": ""})
        check("POST /api/generate rejects empty url", r.status_code == 400, f"status={r.status_code}")

        r = c.post("/api/generate", json={"url": "https://youtu.be/x", "mode": "api", "num_clips": 2})
        job_id = (r.get_json() or {}).get("job_id")
        check("POST /api/generate returns job_id", r.status_code == 202 and bool(job_id),
              f"body={r.get_json()}")

        stages = []
        final = None
        resp = c.get(f"/api/progress/{job_id}")
        for raw in resp.response:
            line = raw.decode()
            if not line.startswith("data: "):
                continue
            evt = json.loads(line[6:])
            st = evt.get("stage") or evt.get("status")
            if st not in stages:
                stages.append(st)
            if evt.get("stage") == "done":
                final = evt
                break
            if evt.get("status") == "error":
                final = evt
                break
        check("SSE streams every stage",
              stages == ["starting", "downloading", "transcribing", "analyzing", "rendering", "done"],
              f"stages={stages}")
        check("SSE done event carries result",
              bool(final and final.get("result", {}).get("shorts")),
              f"shorts={len(final.get('result', {}).get('shorts', [])) if final else 0}")

        job = c.get(f"/api/status/{job_id}").get_json()
        check("status endpoint reports completed",
              job.get("status") == "completed" and job.get("progress") == 100,
              f"status={job.get('status')} progress={job.get('progress')}")

        check("unknown job returns 404", c.get("/api/status/nope").status_code == 404)

        r = c.post("/api/generate", json={"url": "C:/v.mp4", "mode": "local", "num_clips": 1})
        job = wait_for_finish(c, r.get_json()["job_id"])
        url0 = job["result"]["shorts"][0]["clip_url"]
        check("local abs path rewritten to /output/", url0 == "/output/short_01.mp4", f"got={url0}")
        check("failed clip keeps error, no url",
              job["result"]["shorts"][1]["clip_url"] is None and job["result"]["shorts"][1].get("error"),
              f"got={job['result']['shorts'][1]}")

        def boom(**kwargs):
            raise RuntimeError("Whisper produced no segments.")

        webapp.generate_shorts = boom
        r = c.post("/api/generate", json={"url": "https://youtu.be/y"})
        job = wait_for_finish(c, r.get_json()["job_id"])
        check("pipeline exception surfaces as error status",
              job.get("status") == "error" and "Whisper" in (job.get("error") or ""),
              f"status={job.get('status')} error={job.get('error')}")
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
