"""Quick smoke test for upload and whisper settings."""
import sys, os
sys.path.insert(0, '.')

from shorts_generator import settings_store
settings_store.SETTINGS_PATH = 'settings.smoke.json'

import app as webapp

c = webapp.app.test_client()

# Upload
import io
up = c.post("/api/upload", data={
    "video": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42test"), "test.mp4"),
}, content_type="multipart/form-data")
print("upload status:", up.status_code)
uploaded = up.get_json() or {}
print("upload path:", uploaded.get("path", "MISSING"))

# Generate with whisper settings
gen = c.post("/api/generate", json={
    "url": uploaded.get("path", "fake.mp4"),
    "source_type": "file",
    "mode": "local",
    "num_clips": 1,
    "whisper_device": "cpu",
    "whisper_model": "tiny",
})
print("generate status:", gen.status_code)

# Check persistence
saved = settings_store.load()
print("whisper_device saved:", saved.get("whisper_device"))
print("whisper_model saved:", saved.get("whisper_model"))
print("source_type saved:", saved.get("source_type"))

os.path.exists('settings.smoke.json') and os.remove('settings.smoke.json')
if uploaded.get("path") and os.path.exists(uploaded["path"]):
    os.remove(uploaded["path"])
print("SMOKE TEST OK")
