"""Checks for the new review/save flow:

  - _run_local renders drafts in 16:9 with finalize=False and stamps
    draft_aspect / target_aspect on every short.
  - POST /api/shorts/save happy path: reframes (stubbed) then finalizes
    (stubbed) into output/saved/<subfolder>/, returns {ok, saved, url},
    and the draft file is gone.
  - Failure path: _reframe_vertical raising -> 500, draft bytes intact.
  - Already-saved clip (/output/saved/...) -> 400 (idempotence guard).
  - Overwrite rule: saving two drafts with the same basename into one saved/
    folder never fails; the second version wins.
  - POST /api/shorts/delete: removes the file, 404 when gone, rejects
    non-/output urls and traversal.
  - Saved listing: job_shorts marks a clip in output/saved/ as saved=True.

Everything heavy (ffmpeg, reframe, finalize) is stubbed; no network.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import settings_store  # noqa: E402

# Use a scratch settings file so a real one is never touched.
settings_store.SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.test.json"
)

from shorts_generator import config as cfg  # noqa: E402
from shorts_generator import pipeline  # noqa: E402
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


def run_pipeline_draft_checks():
    """_run_local must render drafts at 16:9, finalize=False, and stamp aspects.

    pipeline._run_local imports crop_highlights_local/download_youtube_local/
    transcribe_local/make_local_llm_fn from their source submodules *inside*
    the function body, so we swap the symbols on those source modules.
    """
    from shorts_generator.local import clipper as _c
    from shorts_generator.local import downloader as _d
    from shorts_generator.local import llm as _l
    from shorts_generator.local import transcriber as _t

    rec = Recorder(fn=lambda *a, **k: [{"title": "A", "start_time": 0, "end_time": 5,
                                        "score": 9, "clip_url": "x.mp4"}])
    real = {
        "crop": _c.crop_highlights_local, "dl": _d.download_youtube_local,
        "info": _d.get_last_download_info, "tr": _t.transcribe_local,
        "llm": _l.make_local_llm_fn, "hl": pipeline.get_highlights,
    }
    try:
        _c.crop_highlights_local = rec
        _d.download_youtube_local = lambda *a, **k: os.devnull
        _d.get_last_download_info = lambda: {"folder": "video"}
        _t.transcribe_local = lambda *a, **k: {"segments": [{"start": 0, "end": 5, "text": "hi"}],
                                                "text": "hi", "words": []}
        _l.make_local_llm_fn = lambda p=None: (lambda *a, **k: None)
        pipeline.get_highlights = lambda *a, **k: {"highlights": [
            {"title": "A", "start_time": 0, "end_time": 5, "score": 9}]}

        result = pipeline._run_local("http://youtu", 1, "9:16", "best", None)
        calls = rec.calls
        ok_aspect = len(calls) >= 1 and calls[0][1].get("aspect_ratio") == "16:9"
        ok_finalize = len(calls) >= 1 and calls[0][1].get("finalize") is False
        check("pipeline draft: crop called with aspect_ratio=16:9", ok_aspect,
              f"calls={[(c[1].get('aspect_ratio'), c[1].get('finalize')) for c in calls]}")
        check("pipeline draft: crop called with finalize=False", ok_finalize)
        shorts = result.get("shorts") or []
        stamped = bool(shorts) and all(
            s.get("draft_aspect") == "16:9" and s.get("target_aspect") == "9:16"
            for s in shorts)
        check("pipeline draft: shorts stamped draft_aspect/target_aspect", stamped,
              f"shorts={shorts}")
    finally:
        _c.crop_highlights_local = real["crop"]
        _d.download_youtube_local = real["dl"]
        _d.get_last_download_info = real["info"]
        _t.transcribe_local = real["tr"]
        _l.make_local_llm_fn = real["llm"]
        pipeline.get_highlights = real["hl"]


def _mk_job(draft_abs):
    return {
        "status": "completed", "stage": "done", "progress": 100,
        "url": "http://youtu", "aspect_ratio": "9:16", "mode": "local",
        "llm_provider": None,
        "_params": {
            "mode": "local", "aspect_ratio": "9:16", "api_keys": {},
            "overlay_enabled": "0", "music_enabled": "0", "blur_bars": "1",
            "silence_cut": "0", "captions_enabled": "1",
            "caption_style": "karaoke", "face_track": "1",
        },
        "result": {"shorts": [{"clip_url": draft_abs, "title": "A",
                                "draft_aspect": "16:9", "target_aspect": "9:16"}]},
    }


def run_save_endpoint_checks():
    client = webapp.app.test_client()
    real_reframe = clip._reframe_vertical
    real_finalize = clip.finalize_clip_local

    # Scratch dir must live under output/ so _resolve_output_safe can resolve it.
    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="review-flow-", dir=uploads)
    try:
        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")

        def new_draft(name="draft_01.mp4", body=b"draftbytes"):
            p = os.path.join(tmp, name)
            with open(p, "wb") as f:
                f.write(body)
            return p

        # --- happy path ---
        draft_abs = new_draft()
        draft_rel = f"{rel_dir}/draft_01.mp4"
        draft_url = f"/output/{draft_rel}"

        def reframe_writes(in_path, out_path, aspect):
            assert in_path == os.path.realpath(draft_abs), f"reframe in={in_path}"
            with open(out_path, "wb") as f:
                f.write(b"reframed:" + aspect.encode())
            return out_path

        def finalize_touches(path, aspect, captions_ass=None):
            with open(path, "ab") as f:
                f.write(b"+fx")

        clip._reframe_vertical = Recorder(fn=reframe_writes)
        clip.finalize_clip_local = Recorder(fn=finalize_touches)

        webapp.jobs["job-happy"] = _mk_job(os.path.realpath(draft_abs))
        try:
            r = client.post("/api/shorts/save", json={"url": draft_url})
            body = r.get_json() or {}
            check("save: draft -> 200 {ok,saved}", r.status_code == 200
                  and body.get("ok") is True and body.get("saved") is True,
                  f"status={r.status_code} body={body}")
            # 9:16 + blur_bars on (the job's params): the draft is ALREADY
            # landscape, so save_short skips the reframe and hands the draft
            # straight to a blur-pad -- reframing first would stretch the fg
            # back over the whole canvas and leave no bars (the shipped bug).
            re_calls = clip._reframe_vertical.calls
            check("save: blur bars on -> reframe SKIPPED (blurpad letterboxes)",
                  re_calls == [], f"calls={re_calls}")
            # finalize called on the tmp with same aspect
            fi = clip.finalize_clip_local.calls
            check("save: finalize called once with aspect", len(fi) == 1 and fi[0][0][1] == "9:16",
                  f"calls={fi}")
            # file moved under output/saved/<reldir>/, draft deleted
            saved_url = body.get("url") or ""
            check("save: returned url under /output/saved/", saved_url.startswith("/output/saved/"),
                  f"url={saved_url}")
            saved_abs = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR),
                                      "saved", rel_dir, "draft_01.mp4")
            check("save: final file exists in saved/", os.path.isfile(saved_abs), saved_abs)
            check("save: draft removed", not os.path.exists(draft_abs))
            with open(saved_abs, "rb") as f:
                content = f.read()
            check("save: final file = draft bytes + fx (no reframe in blur mode)",
                  content == b"draftbytes+fx", f"bytes={content!r}")
        finally:
            webapp.jobs.pop("job-happy", None)

        # --- failure path: blur stage blows up in finalize -> 500, draft ok ---
        draft_abs2 = new_draft("draft_02.mp4")
        draft_rel2 = f"{rel_dir}/draft_02.mp4"
        draft_url2 = f"/output/{draft_rel2}"

        def finalize_raises(path, aspect, captions_ass=None):
            with open(path, "ab") as f:
                f.write(b"partial")
            raise RuntimeError("face-track exploded")

        clip._reframe_vertical = Recorder()
        clip.finalize_clip_local = Recorder(fn=finalize_raises)
        webapp.jobs["job-fail"] = _mk_job(os.path.realpath(draft_abs2))
        try:
            r = client.post("/api/shorts/save", json={"url": draft_url2})
            body = r.get_json() or {}
            check("save(fail): 500 {error}", r.status_code == 500
                  and "face-track exploded" in (body.get("error") or ""),
                  f"status={r.status_code} body={body}")
            with open(draft_abs2, "rb") as f:
                check("save(fail): draft bytes intact", f.read() == b"draftbytes")
            check("save(fail): tmp sibling swept", not os.path.exists(draft_abs2 + ".tmp_save.mp4"))
        finally:
            webapp.jobs.pop("job-fail", None)

        # --- idempotence: already in saved/ -> 400 ---
        saved_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir)
        os.makedirs(saved_dir, exist_ok=True)
        saved_clip = os.path.join(saved_dir, "already.mp4")
        with open(saved_clip, "wb") as f:
            f.write(b"x")
        saved_url = f"/output/saved/{rel_dir}/already.mp4"
        clip._reframe_vertical = Recorder()
        r = client.post("/api/shorts/save", json={"url": saved_url})
        check("save: already-saved -> 400", r.status_code == 400, f"status={r.status_code}")
        check("save: already-saved — reframe not called", clip._reframe_vertical.calls == [])

        # --- bad url -> 400, traversal -> 400, missing -> 404 ---
        clip._reframe_vertical = Recorder()
        r = client.post("/api/shorts/save", json={"url": "https://cdn.example.com/x.mp4"})
        check("save: non-/output url -> 400", r.status_code == 400)
        r = client.post("/api/shorts/save", json={"url": "/output/../escape.mp4"})
        check("save: traversal -> 400", r.status_code == 400)
        r = client.post("/api/shorts/save", json={"url": f"/output/{rel_dir}/nope.mp4"})
        check("save: missing file -> 404", r.status_code == 404)
    finally:
        clip._reframe_vertical = real_reframe
        clip.finalize_clip_local = real_finalize
        shutil.rmtree(tmp, ignore_errors=True)
        # clean the saved scratch tree
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)


def run_save_overwrite_check():
    """Saving two drafts with the same basename into one saved/ folder must not
    fail or silently keep the first version: the second save overwrites."""
    client = webapp.app.test_client()
    real_reframe = clip._reframe_vertical
    real_finalize = clip.finalize_clip_local

    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="review-overwrite-", dir=uploads)
    try:
        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        clip._reframe_vertical = Recorder(
            fn=lambda i, o, a: open(o, "wb").write(open(i, "rb").read()))
        clip.finalize_clip_local = Recorder(fn=lambda p, a, captions_ass=None: open(p, "ab").write(b"+fx"))

        for body in (b"v1", b"v2"):
            draft_abs = os.path.join(tmp, "clip.mp4")
            with open(draft_abs, "wb") as f:
                f.write(body)
            job_id = f"job-ow-{body.decode()}"
            webapp.jobs[job_id] = _mk_job(os.path.realpath(draft_abs))
            try:
                r = client.post("/api/shorts/save",
                                json={"url": f"/output/{rel_dir}/clip.mp4"})
                check(f"save(overwrite {body.decode()}): 200", r.status_code == 200,
                      f"status={r.status_code} body={r.get_json()}")
            finally:
                webapp.jobs.pop(job_id, None)

        saved_abs = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR),
                                 "saved", rel_dir, "clip.mp4")
        with open(saved_abs, "rb") as f:
            content = f.read()
        check("save(overwrite): second version wins, no error",
              content == b"v2+fx", f"bytes={content!r}")
    finally:
        clip._reframe_vertical = real_reframe
        clip.finalize_clip_local = real_finalize
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)


def run_save_captions_sidecar_check():
    """The caption sidecar must survive a save: passed to finalize (so the
    burn finds it despite the tmp name) and moved alongside the clip into
    saved/ afterwards."""
    client = webapp.app.test_client()
    real_reframe = clip._reframe_vertical
    real_finalize = clip.finalize_clip_local

    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="review-caps-", dir=uploads)
    try:
        rel_dir = os.path.relpath(tmpdir, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        draft_abs = os.path.join(tmpdir, "cap.mp4")
        with open(draft_abs, "wb") as f:
            f.write(b"draft")
        sidecar = draft_abs + ".ass"
        with open(sidecar, "w", encoding="utf-8") as f:
            f.write("[Script Info]\n")

        clip._reframe_vertical = Recorder(
            fn=lambda i, o, a: open(o, "wb").write(open(i, "rb").read()))
        # finalize with a captions_ass kwarg must burn (append) and NOT touch sidecar
        def fake_finalize(path, aspect, captions_ass=None):
            with open(path, "ab") as f:
                f.write(b"+fx")
            return path
        fin = Recorder(fn=fake_finalize)
        clip.finalize_clip_local = fin

        webapp.jobs["job-caps"] = _mk_job(os.path.realpath(draft_abs))
        try:
            r = client.post("/api/shorts/save",
                            json={"url": f"/output/{rel_dir}/cap.mp4"})
            body = r.get_json() or {}
            check("save(captions): 200", r.status_code == 200,
                  f"status={r.status_code} body={body}")
            calls = fin.calls
            kw = calls[0][1] if calls else {}
            check("save(captions): finalize got captions_ass",
                  bool(calls) and kw.get("captions_ass") == draft_abs + ".ass",
                  f"calls={calls}")
            saved_ass = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR),
                                      "saved", rel_dir, "cap.mp4.ass")
            check("save(captions): sidecar moved into saved/",
                  os.path.isfile(saved_ass), saved_ass)
            check("save(captions): sidecar gone from draft dir",
                  not os.path.exists(sidecar))
        finally:
            webapp.jobs.pop("job-caps", None)
    finally:
        clip._reframe_vertical = real_reframe
        clip.finalize_clip_local = real_finalize
        shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)


def run_delete_endpoint_checks():
    """«Удалить» → POST /api/shorts/delete: file gone; missing file -> 404;
    non-/output url and traversal -> 400."""
    client = webapp.app.test_client()
    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="review-delete-", dir=uploads)
    try:
        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        victim = os.path.join(tmp, "dead.mp4")
        with open(victim, "wb") as f:
            f.write(b"bye")

        r = client.post("/api/shorts/delete",
                        json={"url": f"/output/{rel_dir}/dead.mp4"})
        check("delete: 200 {ok}", r.status_code == 200
              and (r.get_json() or {}).get("ok") is True,
              f"status={r.status_code} body={r.get_json()}")
        check("delete: file removed", not os.path.exists(victim))

        r = client.post("/api/shorts/delete",
                        json={"url": f"/output/{rel_dir}/dead.mp4"})
        check("delete: missing file -> 404", r.status_code == 404)
        r = client.post("/api/shorts/delete",
                        json={"url": "https://cdn.example.com/x.mp4"})
        check("delete: non-/output url -> 400", r.status_code == 400)
        r = client.post("/api/shorts/delete", json={"url": "/output/../escape.mp4"})
        check("delete: traversal -> 400", r.status_code == 400)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_saved_listing_check():
    """job_shorts must mark a saved clip as saved=True when it lives in output/saved/."""
    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="review-list-", dir=uploads)
    try:
        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        saved_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir)
        os.makedirs(saved_dir, exist_ok=True)
        clip_path = os.path.join(saved_dir, "c1.mp4")
        with open(clip_path, "wb") as f:
            f.write(b"x")
        rel = os.path.relpath(os.path.realpath(clip_path),
                              os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        webapp.jobs["job-list"] = {
            "status": "completed", "stage": "done", "progress": 100,
            "url": "http://youtu", "aspect_ratio": "9:16",
            "result": {"shorts": [{"clip_url": os.path.realpath(clip_path), "title": "A"}]},
        }
        client = webapp.app.test_client()
        try:
            r = client.get("/api/jobs/job-list/shorts")
            body = r.get_json() or {}
            shorts = body.get("shorts") or []
            ok = (r.status_code == 200 and len(shorts) == 1
                  and shorts[0].get("url", "").startswith("/output/saved/")
                  and shorts[0].get("saved") is True)
            check("listing: saved clip surfaced as saved=True under /output/saved/", ok,
                  f"status={r.status_code} shorts={shorts}")
        finally:
            webapp.jobs.pop("job-list", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)


def main():
    run_pipeline_draft_checks()
    run_save_endpoint_checks()
    run_save_overwrite_check()
    run_save_captions_sidecar_check()
    run_delete_endpoint_checks()
    run_saved_listing_check()

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
