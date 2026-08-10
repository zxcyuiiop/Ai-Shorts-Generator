"""Security checks for the web layer (Wave 2 fixes).

Covers, all hermetically (tempdir settings/output dir, stubbed pipeline):
  - POST /api/generate: 400 on a non-integer num_clips; silent clamp to [1,20]
  - SSRF allow-list: a URL source is rejected unless it is http(s) on
    youtube.com (any subdomain, incl. music.youtube.com) or youtu.be;
    local file paths still pass
  - GUI_TOKEN: helper + before_request hook (no token -> API open; token set
    -> 401 without it, 200 with Bearer header or ?token=); / and /output stay
    open either way
  - SSE helper: a terminal job is reported terminal (stream-closing condition)
  - _humanize_error: no server paths in client messages; the yt-dlp
    "Sign in to confirm" hint keeps its friendly text
  - Upload streaming cap: files past MAX_UPLOAD_BYTES are aborted with 413
    and the partial file is removed
  - settings_store: os.chmod failure warns once; .env.example placeholders
    are persisted as empty, not as junk keys
"""
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="security-test-")

from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

import app as webapp  # noqa: E402

webapp.LOCAL_OUTPUT_DIR = _TMP
webapp.UPLOAD_DIR = os.path.join(_TMP, "uploads")
webapp.MUSIC_UPLOAD_DIR = os.path.join(_TMP, "music")

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        failures.append(name)


def fake_pipeline(**kwargs):
    return {"shorts": [], "highlights": []}


def wait_done(client, job_id, tries=100):
    job = {}
    for _ in range(tries):
        job = client.get(f"/api/status/{job_id}").get_json() or {}
        if job.get("status") in ("completed", "error"):
            return job
        time.sleep(0.1)
    return job


def check_is_http_url():
    ok_pairs = [
        ("https://youtu.be/x", True, "youtu.be https URL"),
        ("https://music.youtube.com/watch?v=1", True, "music.youtube.com URL"),
        ("http://evil.com/v.mp4", True, "arbitrary http URL still flagged by guard"),
        ("C:/v.mp4", False, "Windows path with drive slash is NOT a URL"),
        ("C:\\v.mp4", False, "Windows path with backslash is NOT a URL"),
        ("/home/u/v.mp4", False, "POSIX path is NOT a URL"),
        ("~\\v.mp4", False, "home-relative path is NOT a URL"),
        ("file:///etc/passwd", False, "file:// is NOT allowed http"),
    ]
    for value, expected, label in ok_pairs:
        _parsed, got = webapp._parse_url(value)
        check(f"_parse_url is_http_url: {label}", got == expected,
              f"{value!r} -> {got}")


def check_num_clips():
    c = webapp.app.test_client()
    r = c.post("/api/generate",
               json={"url": "https://youtu.be/x", "num_clips": "abc"})
    check("generate: bad num_clips -> 400 (not 500)",
          r.status_code == 400 and "error" in (r.get_json() or {}),
          f"status={r.status_code} body={r.get_json()}")

    r = c.post("/api/generate",
               json={"url": "https://youtu.be/x", "num_clips": 999})
    job_id = (r.get_json() or {}).get("job_id")
    ok_status = r.status_code == 202 and bool(job_id)
    check("generate: num_clips=999 accepted (clamped)", ok_status,
          f"status={r.status_code}")
    if not ok_status:
        return
    with webapp.jobs_lock:
        stored = webapp.jobs[job_id]["_params"].get("num_clips")
    check("generate: num_clips clamped to 20", stored == 20, f"stored={stored}")
    # Drain the job so the worker doesn't outlive the suite.
    client2 = webapp.app.test_client()
    wait_done(client2, job_id)


def check_allow_list():
    # helper unit checks
    check("allow-list: accepts https://youtu.be/abc",
          webapp._is_allowed_video_url("https://youtu.be/abc"))
    check("allow-list: accepts https://music.youtube.com/watch?v=1",
          webapp._is_allowed_video_url("https://music.youtube.com/watch?v=1"))
    check("allow-list: accepts https://www.youtube.com/watch?v=1",
          webapp._is_allowed_video_url("https://www.youtube.com/watch?v=1"))
    check("allow-list: rejects http://evil.com",
          not webapp._is_allowed_video_url("http://evil.com"))
    check("allow-list: rejects python traffic to evil-youtube.com",
          not webapp._is_allowed_video_url("https://evil-youtube.com/watch?v=1"))
    check("allow-list: rejects file:// scheme",
          not webapp._is_allowed_video_url("file:///etc/passwd"))
    check("allow-list: rejects metadata IP",
          not webapp._is_allowed_video_url("http://169.254.169.254/latest/meta-data"))

    c = webapp.app.test_client()
    r = c.post("/api/generate", json={"url": "http://evil.com/v.mp4"})
    check("generate: http://evil.com -> 400",
          r.status_code == 400 and "error" in (r.get_json() or {}),
          f"status={r.status_code} body={r.get_json()}")

    r = c.post("/api/generate", json={"url": "https://youtu.be/alpha"})
    check("generate: youtube URL accepted", r.status_code == 202,
          f"status={r.status_code} body={r.get_json()}")
    if r.status_code == 202:
        wait_done(c, r.get_json()["job_id"])

    # Local path + local mode must not hit the URL check.
    fpath = os.path.join(_TMP, "clip.mp4")
    with open(fpath, "wb") as f:
        f.write(b"x")
    r = c.post("/api/generate",
               json={"url": fpath, "source_type": "file", "mode": "local"})
    check("generate: local file path still accepted in local mode",
          r.status_code == 202, f"status={r.status_code} body={r.get_json()}")
    if r.status_code == 202:
        wait_done(c, r.get_json()["job_id"])


def check_token():
    # Disabled by default: open API.
    webapp._gui_token.override = ""
    c = webapp.app.test_client()
    r = c.get("/api/settings")
    check("token unset: /api/settings open", r.status_code == 200,
          f"status={r.status_code}")

    webapp._gui_token.override = "sekrit"
    try:
        try:
            r = c.get("/api/settings")
            check("token set: /api/settings without token -> 401",
                  r.status_code == 401 and "error" in (r.get_json() or {}),
                  f"status={r.status_code}")

            r = c.get("/api/settings", headers={"Authorization": "Bearer sekrit"})
            check("token set: Bearer header -> 200", r.status_code == 200,
                  f"status={r.status_code}")

            r = c.get("/api/settings?token=sekrit")
            check("token set: ?token= query -> 200", r.status_code == 200,
                  f"status={r.status_code}")

            r = c.get("/api/settings", headers={"Authorization": "Bearer wrong"})
            check("token set: wrong token -> 401", r.status_code == 401,
                  f"status={r.status_code}")

            r = c.get("/")
            check("token set: / (UI) stays open", r.status_code == 200,
                  f"status={r.status_code}")

            clip = os.path.join(_TMP, "pub.mp4")
            with open(clip, "wb") as f:
                f.write(b"x")
            r = c.get("/output/pub.mp4")
            check("token set: /output/ stays open", r.status_code == 200,
                  f"status={r.status_code}")
        finally:
            webapp._gui_token.override = ""
    except Exception:
        webapp._gui_token.override = ""
        raise
    check("token cleared: /api/settings open again",
          c.get("/api/settings").status_code == 200)


def check_terminal_helper():
    check("_is_terminal_job: completed",
          webapp._is_terminal_job({"status": "completed"}))
    check("_is_terminal_job: running is not",
          not webapp._is_terminal_job({"status": "running"}))
    check("_is_terminal_job: missing/None is not",
          not webapp._is_terminal_job(None))

    # SSE stream for an already-terminal job must close immediately after the
    # backlog replay rather than hanging (generator has no more events).
    job_id = "job-security-terminal"
    with webapp.jobs_lock:
        webapp.jobs[job_id] = {
            "status": "error", "stage": "done", "progress": 0,
            "started_at": time.time(), "finished_at": time.time(),
            "added_at": time.time(), "log": [],
        }
    try:
        c = webapp.app.test_client()
        resp = c.get(f"/api/progress/{job_id}")
        body = b"".join(resp.response)  # hangs forever if the stream leaks
        check("SSE: terminal job closes stream immediately", True,
              f"len={len(body)}")
    finally:
        with webapp.jobs_lock:
            webapp.jobs.pop(job_id, None)

    # _finish_progress_queue unblocks a waiting generator.
    import queue as _q
    qq = _q.Queue()
    webapp.progress_queues["job-ssec"] = qq
    try:
        webapp._finish_progress_queue("job-ssec")
        got = qq.get(timeout=1)
        check("_finish_progress_queue pushes sentinel", got == -1, f"got={got}")
    finally:
        webapp.progress_queues.pop("job-ssec", None)


def check_humanize():
    msg = webapp._humanize_error(RuntimeError("kaboom internals"))
    check("humanize: generic error hides details",
          msg == "Ошибка пайплайна: RuntimeError", f"msg={msg}")

    msg = webapp._humanize_error(RuntimeError(
        "Sign in to confirm you're not a bot"))
    check("humanize: yt-dlp bot check keeps its hint",
          "бот" in msg, f"msg={msg}")

    long_path = r"E:\Desktop\secret-project\src\mp4 broke"
    msg = webapp._humanize_error(FileNotFoundError(long_path))
    check("humanize: FileNotFoundError friendly, no full path",
          "Файл не найден" in msg and r"E:\Desktop\secret" not in msg,
          f"msg={msg}")

    msg = webapp._humanize_error(RuntimeError(
        "ffmpeg failed: https://api.svc.test/x?token=topsecret&y=1"))
    check("humanize: query string stripped (no secrets)",
          "topsecret" not in msg and "?…" in msg, f"msg={msg}")


def check_save_short_guard():
    # A foreign file in output/ that no job claims must be refused (404),
    # never modified or deleted by /api/shorts/save.
    out_dir = webapp.LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, "foreign_guard_test.mp4")
    with open(fpath, "wb") as f:
        f.write(b"dummy")
    try:
        c = webapp.app.test_client()
        r = c.post("/api/shorts/save", json={"url": "/output/foreign_guard_test.mp4"})
        check("save: foreign file in output/ rejected with 404", r.status_code == 404,
              f"status={r.status_code} body={r.get_json()}")
        check("save: foreign file untouched", os.path.isfile(fpath))
    finally:
        if os.path.exists(fpath):
            os.remove(fpath)


def check_upload_cap():
    real_cap = webapp.MAX_UPLOAD_BYTES
    webapp.MAX_UPLOAD_BYTES = 128 * 1024  # 128 KB for a fast hermetic check
    try:
        c = webapp.app.test_client()
        payload = io.BytesIO(b"x" * (256 * 1024))  # 256 KB > cap
        r = c.post("/api/upload",
                   data={"video": (payload, "big.mp4")},
                   content_type="multipart/form-data")
        check("upload over cap -> 413", r.status_code == 413,
              f"status={r.status_code}")
        # Partial file must have been removed.
        leftovers = [n for n in os.listdir(webapp.UPLOAD_DIR)
                     if n.endswith("big.mp4")] if os.path.isdir(webapp.UPLOAD_DIR) else []
        check("upload over cap: partial file removed", leftovers == [],
              f"leftovers={leftovers}")

        small = io.BytesIO(b"ok" * 100)
        r = c.post("/api/upload",
                   data={"video": (small, "small.mp4")},
                   content_type="multipart/form-data")
        check("upload under cap -> 200", r.status_code == 200,
              f"status={r.status_code} body={r.get_json()}")
    finally:
        webapp.MAX_UPLOAD_BYTES = real_cap


def check_settings_store():
    # Placeholder values are persisted as empty, not as keys.
    settings_store.save({"muapi_key": "nvapi-your_key_here"})
    stored = settings_store.load()
    check("settings: placeholder key stored as empty",
          stored.get("muapi_key") == "", f"stored={stored.get('muapi_key')!r}")

    settings_store.save({"muapi_key": "real-key-123"})
    check("settings: real key kept",
          settings_store.load().get("muapi_key") == "real-key-123")

    # chmod failure must print a warning exactly once, not be swallowed.
    real_chmod = os.chmod

    def boom(*a, **k):
        raise OSError("EPERM simulated")

    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        settings_store.os.chmod = boom
        settings_store._chmod_warned = False
        sys.stdout = buf
        settings_store.save({"url": "https://youtu.be/w"})
        settings_store.save({"url": "https://youtu.be/w2"})
    finally:
        sys.stdout = old_stdout
        settings_store.os.chmod = real_chmod
        settings_store._chmod_warned = False
    out = buf.getvalue()
    check("settings: chmod failure warns once", out.count("WARNING") == 1,
          f"out={out!r}")

    if os.path.exists(settings_store.SETTINGS_PATH):
        os.remove(settings_store.SETTINGS_PATH)


def main():
    webapp.generate_shorts = fake_pipeline
    try:
        check_num_clips()
        check_is_http_url()
        check_allow_list()
        check_token()
        check_terminal_helper()
        check_humanize()
        check_save_short_guard()
        check_upload_cap()
        check_settings_store()
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
