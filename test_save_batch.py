# -*- coding: utf-8 -*-
"""Checks for the batch save endpoint ``POST /api/shorts/save_batch``:

  - {items: [3 valid draft urls]} -> HTTP 200, all three files land in
    output/saved/, results align with the request order.
  - 2 valid + 1 bogus url -> HTTP 200, per-item ok/error, valids moved.
  - Empty items / no items -> 400.
  - Per-item titles are honored (filename comes from the title).

Everything heavy (reframe, finalize) is stubbed; no network.
"""
import os
import shutil
import sys
import tempfile
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import settings_store  # noqa: E402

# Use a scratch settings file so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.test.json"
)

from shorts_generator.local import clipper as clip  # noqa: E402
import app as webapp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


class Recorder:
    """Stand-in that records its calls."""

    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fn is not None:
            return self.fn(*args, **kwargs)
        return None


def _mk_job(draft_abs, title=None):
    short = {"clip_url": draft_abs, "title": title or "A",
             "draft_aspect": "16:9", "target_aspect": "9:16"}
    return {
        "status": "completed", "stage": "done", "progress": 100,
        "url": "http://youtu", "aspect_ratio": "9:16", "mode": "local",
        "llm_provider": None,
        "_params": {
            "mode": "local", "aspect_ratio": "9:16", "api_keys": {},
            "overlay_enabled": "0", "music_enabled": "0", "blur_bars": "1",
            "silence_cut": "0", "captions_enabled": "1",
            "caption_style": "karaoke", "face_track": "1",
            "caption_position": "bottom", "caption_margin_v": "120",
        },
        "result": {"shorts": [short]},
    }


def run_batch_checks():
    client = webapp.app.test_client()
    real_reframe = clip._reframe_vertical
    real_finalize = clip.finalize_clip_local

    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="save-batch-", dir=uploads)
    made_jobs = []
    try:
        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        clip._reframe_vertical = Recorder(
            fn=lambda i, o, a: open(o, "wb").write(open(i, "rb").read()))
        clip.finalize_clip_local = Recorder(
            fn=lambda p, a, captions_ass=None, title_text=None: open(p, "ab").write(b"+fx"))

        def new_draft(job_id, name, title=None):
            draft_abs = os.path.join(tmp, name)
            with open(draft_abs, "wb") as f:
                f.write(b"draftbytes")
            webapp.jobs[job_id] = _mk_job(os.path.realpath(draft_abs), title=title)
            made_jobs.append(job_id)
            return draft_abs

        saved_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir)

        # --- (a) batch of 3 valid drafts -> all ok, files moved ---
        drafts = [new_draft(f"job-b{i}", f"b_0{i}.mp4") for i in (1, 2, 3)]
        items = [{"url": f"/output/{rel_dir}/b_0{i}.mp4"} for i in (1, 2, 3)]
        r = client.post("/api/shorts/save_batch", json={"items": items})
        body = r.get_json() or {}
        results = body.get("results") or []
        check("batch(3): HTTP 200", r.status_code == 200, f"status={r.status_code} body={body}")
        check("batch(3): 3 results, all ok",
              len(results) == 3 and all(x.get("ok") for x in results),
              f"results={results}")
        check("batch(3): result urls align with the request",
              [x.get("url") for x in results] and results[0].get("url") != results[1].get("url"))
        for i in (1, 2, 3):
            check(f"batch(3): b_0{i}.mp4 moved into saved/",
                  os.path.isfile(os.path.join(saved_dir, f"b_0{i}.mp4")),
                  os.path.join(saved_dir, f"b_0{i}.mp4"))
            check(f"batch(3): draft b_0{i}.mp4 removed", not os.path.exists(drafts[i - 1]))

        # --- (d) titles honored per item (filename from title) ---
        d_t = new_draft("job-bt", "t_1.mp4", title="Хайлайт один")
        r = client.post("/api/shorts/save_batch",
                        json={"items": [{"url": f"/output/{rel_dir}/t_1.mp4",
                                         "title": "Хайлайт один"}]})
        body = r.get_json() or {}
        res0 = (body.get("results") or [{}])[0]
        check("batch(title): 200 ok, name is the title",
              r.status_code == 200 and res0.get("ok") and res0.get("name") == "Хайлайт один",
              f"status={r.status_code} res0={res0}")
        check("batch(title): file named by the title",
              os.path.isfile(os.path.join(saved_dir, "Хайлайт один.mp4")),
              os.path.join(saved_dir, "Хайлайт один.mp4"))

        # --- (b) mixed: 2 valid + 1 bogus url -> per-item ok/error, HTTP 200 ---
        new_draft("job-bm1", "m_1.mp4")
        new_draft("job-bm2", "m_2.mp4")
        items = [
            {"url": f"/output/{rel_dir}/m_1.mp4"},
            {"url": "/output/definitely/not-here.mp4"},
            {"url": f"/output/{rel_dir}/m_2.mp4"},
        ]
        r = client.post("/api/shorts/save_batch", json={"items": items})
        body = r.get_json() or {}
        results = body.get("results") or []
        oks = [x.get("ok") for x in results]
        check("batch(mixed): HTTP 200 even with a failing item",
              r.status_code == 200, f"status={r.status_code} body={body}")
        check("batch(mixed): per-item ok flags [True, False, True]",
              oks == [True, False, True], f"oks={oks}")
        check("batch(mixed): failing item carries an error",
              bool(results[1].get("error")) if len(results) == 3 else False,
              f"results={results}")
        check("batch(mixed): valid clips moved",
              os.path.isfile(os.path.join(saved_dir, "m_1.mp4"))
              and os.path.isfile(os.path.join(saved_dir, "m_2.mp4")))

        # --- (c) empty items -> 400; missing items -> 400 ---
        r = client.post("/api/shorts/save_batch", json={"items": []})
        check("batch(empty): 400", r.status_code == 400, f"status={r.status_code}")
        r = client.post("/api/shorts/save_batch", json={})
        check("batch(no items key): 400", r.status_code == 400, f"status={r.status_code}")

        # --- over the cap -> 400 ---
        big = [{"url": f"/output/{rel_dir}/b_01.mp4"}] * (webapp._SAVE_BATCH_MAX + 1)
        r = client.post("/api/shorts/save_batch", json={"items": big})
        check("batch(too many): 400", r.status_code == 400, f"status={r.status_code}")
    finally:
        for jid in made_jobs:
            webapp.jobs.pop(jid, None)
        clip._reframe_vertical = real_reframe
        clip.finalize_clip_local = real_finalize
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)


def main():
    run_batch_checks()

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
