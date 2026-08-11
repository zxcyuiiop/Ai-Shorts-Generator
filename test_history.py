# -*- coding: utf-8 -*-
"""Checks for the persistent clip history (shorts_generator/history.py +
/api/history endpoints):

  - Module: add/list order, limit, delete, toggle, persistence across a fresh
    read, atomicity under concurrent adds from 4 threads.
  - Flask: save a stub draft -> entry in GET /api/history with title/score;
    favorite toggles; history/delete removes entry + video + thumb files;
    merge_disk_scan picks up a hand-planted untracked mp4 exactly once.
  - /api/shorts/delete also drops the matching history entry.

The history file is redirected into a temp dir *before* app is imported (so the
save flow writes there too), same trick as settings_store.SETTINGS_PATH.
Everything heavy (reframe, finalize, thumbnail) is stubbed; no network.
"""
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shorts_generator import history  # noqa: E402

# Redirect the history store into a scratch dir BEFORE importing app (which
# writes history entries during the stubbed saves below). HISTORY_FILE is read
# at call time from the module attr, so assigning it here covers both.
_tmpdir = tempfile.mkdtemp(prefix="history-test-")
history.HISTORY_FILE = os.path.join(_tmpdir, "history.json")

from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.test.json"
)

from shorts_generator.local import clipper as clip, thumbgen  # noqa: E402
import app as webapp  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def _fresh_store():
    if os.path.exists(history.HISTORY_FILE):
        os.remove(history.HISTORY_FILE)


def run_module_checks():
    _fresh_store()

    # add/list order
    e1 = history.add_clip(title="one", source_title="Src", saved_url="/output/saved/a.mp4",
                          score=7.5, duration_sec=30.0, aspect_ratio="9:16")
    e2 = history.add_clip(title="two", saved_url="/output/saved/b.mp4")
    check("add: entry has all schema keys",
          all(k in e1 for k in ("id", "title", "source_title", "saved_url",
                                "thumb_url", "score", "duration_sec",
                                "aspect_ratio", "created_at", "favorite")),
          f"keys={sorted(e1.keys())}")
    check("add: defaults", e2["thumb_url"] is None and e2["score"] is None
          and e2["favorite"] is False, f"e2={e2}")
    check("add: id is time-based string",
          isinstance(e1["id"], str) and e1["id"].startswith("h"))
    lst = history.list_history()
    check("list: newest first", [c["id"] for c in lst] == [e2["id"], e1["id"]],
          f"order={[c['title'] for c in lst]}")
    check("list: limit honored", len(history.list_history(limit=1)) == 1)

    # toggle
    t = history.toggle_favorite(e1["id"])
    check("toggle: False->True", t is not None and t["favorite"] is True)
    t2 = history.toggle_favorite(e1["id"])
    check("toggle: True->False", t2 is not None and t2["favorite"] is False)
    check("toggle: unknown id -> None", history.toggle_favorite("nope") is None)

    # persistence across a fresh read (no module reload needed: every API
    # reloads from disk, so an empty new file would show nothing; here the
    # file on disk holds exactly what we wrote)
    with open(history.HISTORY_FILE, encoding="utf-8") as f:
        on_disk = json.load(f)
    check("persist: both entries in file",
          len(on_disk["clips"]) == 2, f"file={on_disk}")

    # delete
    check("delete: True for existing", history.delete_clip(e2["id"]) is True)
    check("delete: False for missing", history.delete_clip("nope") is False)
    check("delete: actually gone",
          [c["id"] for c in history.list_history()] == [e1["id"]])

    # corrupt file -> empty store + tolerant load
    with open(history.HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("corrupt: list yields empty", history.list_history() == [])
    e = history.add_clip(title="recovered", saved_url="/output/saved/x.mp4")
    check("corrupt: store rewritten on next add",
          [c["id"] for c in history.list_history()] == [e["id"]])

    # atomicity smoke: 4 threads x 10 adds -> all 41 entries present, file parses
    _fresh_store()
    history.add_clip(title="seed", saved_url="/output/saved/seed.mp4")

    def worker(n):
        for i in range(10):
            history.add_clip(title=f"t{n}-{i}", saved_url=f"/output/saved/t{n}-{i}.mp4")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with open(history.HISTORY_FILE, encoding="utf-8") as f:
        data = json.load(f)  # parses -> writes were atomic (no torn file)
    check("threads: all 41 entries present", len(data["clips"]) == 41,
          f"got {len(data['clips'])}")
    check("threads: ids unique",
          len({c["id"] for c in data["clips"]}) == 41)


def _mk_job(draft_abs, title=None, score=8.75, duration=42.0):
    short = {"clip_url": draft_abs, "title": title or "A",
             "score": score, "duration": duration,
             "draft_aspect": "16:9", "target_aspect": "9:16"}
    return {
        "status": "completed", "stage": "done", "progress": 100,
        "url": "http://youtu", "aspect_ratio": "9:16", "mode": "local",
        "llm_provider": None,
        "_params": {"mode": "local", "aspect_ratio": "9:16", "api_keys": {},
                    "source_title": "Исходное видео"},
        "result": {"shorts": [short]},
    }


def run_app_checks():
    _fresh_store()
    client = webapp.app.test_client()
    real_reframe = clip._reframe_vertical
    real_finalize = clip.finalize_clip_local
    real_thumb = thumbgen.make_thumbnail

    uploads = os.path.abspath(webapp.UPLOAD_DIR)
    os.makedirs(uploads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="history-app-", dir=uploads)
    made_jobs = []
    try:
        def _fake_reframe(i, o, a):
            with open(i, "rb") as fi, open(o, "wb") as fo:
                fo.write(fi.read())

        def _fake_thumb(video_path, out_path=None, **kw):
            with open(out_path, "wb") as f:
                f.write(b"\xff\xd8\xff")
            return out_path

        clip._reframe_vertical = _fake_reframe
        clip.finalize_clip_local = lambda p, a, captions_ass=None, title_text=None: None
        # app.py imports make_thumbnail *inside* the helper (from ... import),
        # which re-reads the module attr every call -- patching the module
        # attribute is enough.
        thumbgen.make_thumbnail = _fake_thumb

        rel_dir = os.path.relpath(tmp, os.path.abspath(webapp.LOCAL_OUTPUT_DIR)).replace("\\", "/")
        saved_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir)
        thumb_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "thumbs")

        def new_draft(job_id, name):
            draft_abs = os.path.join(tmp, name)
            with open(draft_abs, "wb") as f:
                f.write(b"draftbytes")
            webapp.jobs[job_id] = _mk_job(os.path.realpath(draft_abs), title="Хайлайт")
            made_jobs.append(job_id)
            return draft_abs

        # (a) save a stub draft -> entry appears in /api/history
        new_draft("job-h1", "h_01.mp4")
        r = client.post("/api/shorts/save",
                        json={"url": f"/output/{rel_dir}/h_01.mp4", "title": "Хайлайт"})
        body = r.get_json() or {}
        check("save: 200 ok", r.status_code == 200 and body.get("ok") is True,
              f"status={r.status_code} body={body}")
        check("save: history_id in response", isinstance(body.get("history_id"), str)
              and body["history_id"].startswith("h"), f"body={body}")
        hid = body.get("history_id")

        r = client.get("/api/history")
        clips = (r.get_json() or {}).get("clips") or []
        entry = next((c for c in clips if c.get("id") == hid), None)
        check("get: entry present", entry is not None,
              f"clips={[(c.get('id'), c.get('title')) for c in clips]}")
        if entry:
            check("get: title from saved filename", entry.get("title") == "Хайлайт",
                  f"title={entry.get('title')!r}")
            check("get: score carried from job", entry.get("score") == 8.75,
                  f"score={entry.get('score')!r}")
            check("get: source_title carried", entry.get("source_title") == "Исходное видео",
                  f"src={entry.get('source_title')!r}")
            check("get: thumb_url points into /output/thumbs",
                  isinstance(entry.get("thumb_url"), str)
                  and entry["thumb_url"].startswith("/output/thumbs/"),
                  f"thumb={entry.get('thumb_url')!r}")
            thumb_rel = (entry.get("thumb_url") or "")[len("/output/"):]
            thumb_abs = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), thumb_rel)
            check("get: thumb file on disk", os.path.isfile(thumb_abs), thumb_abs)
            saved_url = entry.get("saved_url")
            saved_abs = os.path.join(saved_dir, "Хайлайт.mp4")
            check("get: saved file on disk", os.path.isfile(saved_abs))

        # (b) favorite toggles via the endpoint
        r = client.post("/api/history/favorite", json={"id": hid})
        efav = r.get_json() or {}
        check("favorite: 200 + favorite True",
              r.status_code == 200 and efav.get("favorite") is True, f"r={efav}")
        check("favorite: persisted",
              next(c for c in history.list_history() if c["id"] == hid)["favorite"] is True)
        r = client.post("/api/history/favorite", json={"id": "nope"})
        check("favorite: unknown id -> 404", r.status_code == 404)
        r = client.post("/api/history/favorite", json={"id": hid})
        check("favorite: toggles back to False",
              (r.get_json() or {}).get("favorite") is False)

        # (c) merge_disk_scan picks up a hand-planted untracked mp4 once
        plant_dir = os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir)
        os.makedirs(plant_dir, exist_ok=True)
        plant_path = os.path.join(plant_dir, "untracked.mp4")
        with open(plant_path, "wb") as f:
            f.write(b"planted")
        before = len(history.list_history())
        r = client.get("/api/history")   # lazy merge happens here
        clips = (r.get_json() or {}).get("clips") or []
        found = [c for c in clips if c.get("saved_url", "").endswith("untracked.mp4")]
        check("merge: planted file picked up once",
              len(found) == 1 and len(history.list_history()) == before + 1,
              f"found={[(c.get('title'), c.get('score')) for c in found]}")
        if found:
            check("merge: backfill has no score/thumb",
                  found[0].get("score") is None and found[0].get("thumb_url") is None)
            check("merge: title from filename", found[0].get("title") == "untracked")
        # second scan does not duplicate
        client.get("/api/history")
        check("merge: idempotent (no dup on 2nd scan)",
              len(history.list_history()) == before + 1)

        # (d) /api/history/delete removes entry + video + thumb from disk
        r = client.post("/api/history/delete", json={"id": hid})
        check("history delete: 200 ok", r.status_code == 200 and (r.get_json() or {}).get("ok") is True)
        check("history delete: entry gone",
              next((c for c in history.list_history() if c["id"] == hid), None) is None)
        check("history delete: video file deleted", not os.path.exists(saved_abs))
        check("history delete: thumb file deleted", not os.path.exists(thumb_abs))
        r = client.post("/api/history/delete", json={"id": "nope"})
        check("history delete: unknown id -> 404", r.status_code == 404)

        # (e) deleting via /api/shorts/delete cleans the history entry too
        new_draft("job-h2", "h_02.mp4")
        r = client.post("/api/shorts/save",
                        json={"url": f"/output/{rel_dir}/h_02.mp4", "title": "B"})
        hid2 = (r.get_json() or {}).get("history_id")
        saved2_url = f"/output/saved/{rel_dir}/B.mp4"
        check("shorts delete: entry present before", hid2 is not None and
              next((c for c in history.list_history() if c["id"] == hid2), None) is not None)
        r = client.post("/api/shorts/delete", json={"url": saved2_url})
        check("shorts delete: 200", r.status_code == 200)
        check("shorts delete: history entry cleaned",
              next((c for c in history.list_history() if c["id"] == hid2), None) is None)

        # cleanup planted file from merge store
        pid = [c for c in history.list_history() if c.get("saved_url", "").endswith("untracked.mp4")]
        if pid:
            client.post("/api/history/delete", json={"id": pid[0]["id"]})
    finally:
        for jid in made_jobs:
            webapp.jobs.pop(jid, None)
        clip._reframe_vertical = real_reframe
        clip.finalize_clip_local = real_finalize
        thumbgen.make_thumbnail = real_thumb
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "saved", rel_dir),
                      ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.abspath(webapp.LOCAL_OUTPUT_DIR), "thumbs"),
                      ignore_errors=True)


def main():
    run_module_checks()
    run_app_checks()

    if os.path.exists(settings_store.SETTINGS_PATH):
        os.remove(settings_store.SETTINGS_PATH)
    shutil.rmtree(_tmpdir, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
