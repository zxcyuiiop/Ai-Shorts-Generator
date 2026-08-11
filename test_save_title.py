# -*- coding: utf-8 -*-
"""Checks for saving a draft under its highlight title:

  - _safe_title_name: strips Windows-forbidden / control chars, collapses
    whitespace, keeps Cyrillic, truncates, falls back to "".
  - POST /api/shorts/save with title="Мой проект взломали!*" -> the clip lands
    in saved/ as "Мой проект взломали!.mp4" (forbidden char stripped), the
    response carries url + name.
  - Second save with the same title -> "<title>_2.mp4".
  - No title -> the draft basename is preserved (backwards compatible).

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
from shorts_generator.naming import _safe_title_name  # noqa: E402
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


def run_sanitizer_checks():
    check("sanitize: forbidden chars stripped, Cyrillic kept",
          _safe_title_name("Мой проект взломали!*") == "Мой проект взломали!",
          repr(_safe_title_name("Мой проект взломали!*")))
    check("sanitize: all forbidden -> ''",
          _safe_title_name('<>:"/\\|?*') == "")
    check("sanitize: whitespace collapsed",
          _safe_title_name("  a\t b  c  ") == "a b c")
    check("sanitize: trailing dots/spaces trimmed",
          _safe_title_name("name... ") == "name")
    check("sanitize: truncates to max_len",
          _safe_title_name("x" * 100, max_len=80) == "x" * 80)
    check("sanitize: non-str -> ''", _safe_title_name(None) == "")


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


def run_save_title_checks():
    client = webapp.app.test_client()
    real_reframe = clip._reframe_vertical
    real_finalize = clip.finalize_clip_local

    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="save-title-", dir=uploads)
    try:
        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        clip._reframe_vertical = Recorder(
            fn=lambda i, o, a: open(o, "wb").write(open(i, "rb").read()))
        clip.finalize_clip_local = Recorder(fn=lambda p, a, captions_ass=None: open(p, "ab").write(b"+fx"))

        def new_draft(job_id, name, title=None):
            draft_abs = os.path.join(tmp, name)
            with open(draft_abs, "wb") as f:
                f.write(b"draftbytes")
            webapp.jobs[job_id] = _mk_job(os.path.realpath(draft_abs), title=title)
            return draft_abs

        saved_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir)
        title = "Мой проект взломали!*"       # '*' is Windows-forbidden -> stripped
        safe = "Мой проект взломали!"

        # --- (a) save with a Cyrillic title containing a forbidden char ---
        draft_abs = new_draft("job-t1", "draft_01.mp4", title=title)
        try:
            r = client.post("/api/shorts/save", json={"url": f"/output/{rel_dir}/draft_01.mp4",
                                                      "title": title})
            body = r.get_json() or {}
            check("save(title): 200 ok", r.status_code == 200 and body.get("ok") is True,
                  f"status={r.status_code} body={body}")
            check("save(title): name is the sanitized title",
                  body.get("name") == safe, f"name={body.get('name')!r}")
            check("save(title): url contains the sanitized title",
                  safe in unquote(body.get("url") or "") and "*" not in unquote(body.get("url") or ""),
                  f"url={body.get('url')!r}")
            check("save(title): file exists under the sanitized name",
                  os.path.isfile(os.path.join(saved_dir, safe + ".mp4")),
                  os.path.join(saved_dir, safe + ".mp4"))
            check("save(title): draft removed", not os.path.exists(draft_abs))
        finally:
            webapp.jobs.pop("job-t1", None)

        # --- (b) same title again -> _2 suffix ---
        draft_abs2 = new_draft("job-t2", "draft_02.mp4", title=title)
        try:
            r = client.post("/api/shorts/save", json={"url": f"/output/{rel_dir}/draft_02.mp4",
                                                      "title": title})
            body = r.get_json() or {}
            check("save(title dup): 200 ok", r.status_code == 200,
                  f"status={r.status_code} body={body}")
            check("save(title dup): name gets _2 suffix",
                  body.get("name") == safe + "_2", f"name={body.get('name')!r}")
            check("save(title dup): _2 file exists",
                  os.path.isfile(os.path.join(saved_dir, safe + "_2.mp4")))
            check("save(title dup): first file untouched",
                  os.path.isfile(os.path.join(saved_dir, safe + ".mp4")))
        finally:
            webapp.jobs.pop("job-t2", None)

        # --- (c) no title -> draft basename preserved ---
        draft_abs3 = new_draft("job-t3", "draft_03.mp4")
        try:
            r = client.post("/api/shorts/save", json={"url": f"/output/{rel_dir}/draft_03.mp4"})
            body = r.get_json() or {}
            check("save(no title): 200 ok", r.status_code == 200,
                  f"status={r.status_code} body={body}")
            check("save(no title): basename preserved",
                  os.path.isfile(os.path.join(saved_dir, "draft_03.mp4"))
                  and body.get("name") == "draft_03",
                  f"name={body.get('name')!r}")
        finally:
            webapp.jobs.pop("job-t3", None)

        # --- (d) nothing usable in the title -> falls back to the basename ---
        draft_abs4 = new_draft("job-t4", "draft_04.mp4")
        try:
            r = client.post("/api/shorts/save", json={"url": f"/output/{rel_dir}/draft_04.mp4",
                                                      "title": '<>:"/\\|?*'})
            body = r.get_json() or {}
            check("save(bad title): 200 ok, basename preserved",
                  r.status_code == 200
                  and os.path.isfile(os.path.join(saved_dir, "draft_04.mp4"))
                  and body.get("name") == "draft_04",
                  f"status={r.status_code} body={body}")
        finally:
            webapp.jobs.pop("job-t4", None)

        # --- (e) caption sidecar is renamed to match the titled clip ---
        draft_abs5 = new_draft("job-t5", "cap.mp4", title="Хайлайт")
        with open(draft_abs5 + ".ass", "w", encoding="utf-8") as f:
            f.write("[Script Info]\n")
        try:
            r = client.post("/api/shorts/save", json={"url": f"/output/{rel_dir}/cap.mp4",
                                                      "title": "Хайлайт"})
            check("save(title+ass): 200 ok", r.status_code == 200,
                  f"status={r.status_code} body={r.get_json()}")
            check("save(title+ass): sidecar renamed to <title>.mp4.ass",
                  os.path.isfile(os.path.join(saved_dir, "Хайлайт.mp4.ass")))
            check("save(title+ass): sidecar gone from the draft dir",
                  not os.path.exists(draft_abs5 + ".ass"))
        finally:
            webapp.jobs.pop("job-t5", None)
    finally:
        clip._reframe_vertical = real_reframe
        clip.finalize_clip_local = real_finalize
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)


def run_shorts_listing_title_check():
    """job_shorts surfaces the highlight title so the review UI can send it."""
    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="save-title-list-", dir=uploads)
    try:
        clip_path = os.path.join(tmp, "c1.mp4")
        with open(clip_path, "wb") as f:
            f.write(b"x")
        webapp.jobs["job-list-title"] = _mk_job(os.path.realpath(clip_path),
                                                title="Тестовый заголовок")
        client = webapp.app.test_client()
        try:
            r = client.get("/api/jobs/job-list-title/shorts")
            body = r.get_json() or {}
            shorts = body.get("shorts") or []
            ok = (r.status_code == 200 and len(shorts) == 1
                  and shorts[0].get("title") == "Тестовый заголовок")
            check("listing: highlight title surfaced", ok,
                  f"status={r.status_code} shorts={shorts}")
        finally:
            webapp.jobs.pop("job-list-title", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    run_sanitizer_checks()
    run_save_title_checks()
    run_shorts_listing_title_check()

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
