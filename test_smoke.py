"""Quick smoke test for upload and whisper settings.

Hermetic: the settings file and the uploads/output dirs are redirected into a
fresh tempdir before app.py is imported, so a run can never touch the real
settings.local.json or the real output/ tree. The pipeline itself is stubbed
(no ffmpeg, whisper, LLM, or network). The tempdir is removed on exit.
"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="smoke-test-")

from shorts_generator import settings_store  # noqa: E402

settings_store.SETTINGS_PATH = os.path.join(_TMP, "settings.local.json")

import app as webapp  # noqa: E402

# Send every write the app can do into the tempdir, not the real output/.
webapp.LOCAL_OUTPUT_DIR = _TMP
webapp.UPLOAD_DIR = os.path.join(_TMP, "uploads")
webapp.MUSIC_UPLOAD_DIR = os.path.join(_TMP, "music")


def fake_generate_shorts(**kwargs):
    """Stand-in: enough of a result for /api/status; no heavy work at all."""
    return {
        "mode": kwargs.get("mode"),
        "source_video_url": kwargs.get("youtube_url"),
        "transcript": {"duration": 1.0, "segments": []},
        "highlights": [],
        "shorts": [],
    }


def main():
    failures = []

    def check(name, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
        if not cond:
            failures.append(name)

    webapp.generate_shorts = fake_generate_shorts
    c = webapp.app.test_client()

    try:
        # Upload a fake mp4; it must land inside the tempdir, not output/uploads.
        up = c.post("/api/upload", data={
            "video": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42test"), "test.mp4"),
        }, content_type="multipart/form-data")
        uploaded = up.get_json() or {}
        check("upload accepted", up.status_code == 200, f"status={up.status_code}")
        check("upload returns a path", bool(uploaded.get("path")), str(uploaded))
        check("upload landed in tempdir, not the repo output/",
              os.path.realpath(uploaded.get("path", "")).startswith(os.path.realpath(_TMP)),
              uploaded.get("path", "MISSING"))

        # Generate in local mode against the uploaded file; the form's whisper
        # settings must be persisted to the (temp) settings store.
        gen = c.post("/api/generate", json={
            "url": uploaded.get("path", "fake.mp4"),
            "source_type": "file",
            "mode": "local",
            "num_clips": 1,
            "whisper_device": "cpu",
            "whisper_model": "tiny",
        })
        check("generate accepted", gen.status_code == 202, f"status={gen.status_code}")
        check("generate returns a job_id", bool((gen.get_json() or {}).get("job_id")))

        saved = settings_store.load()
        check("whisper_device persisted", saved.get("whisper_device") == "cpu",
              str(saved.get("whisper_device")))
        check("whisper_model persisted", saved.get("whisper_model") == "tiny",
              str(saved.get("whisper_model")))
        check("source_type persisted", saved.get("source_type") == "file",
              str(saved.get("source_type")))
        check("real settings.local.json never touched",
              not os.path.exists("settings.smoke.json"))
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("SMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
