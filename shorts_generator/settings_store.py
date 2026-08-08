"""Persist GUI settings and API keys to disk so they survive a restart.

Written to settings.local.json in the project root -- gitignored, same idea as
.env, but editable from the browser. Keys are stored in plaintext: this is a
localhost single-user tool, and .env already sits next to it in plaintext. The
file is created with owner-only permissions where the OS supports it.

Secret values are never sent back to the browser in full; see mask_secrets().
"""
import json
import os
import stat
import threading
from typing import Dict

SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "settings.local.json",
)

# Fields treated as secrets: masked on read, and a masked value sent back on
# save is ignored rather than overwriting the stored key.
SECRET_FIELDS = {
    "muapi_key",
    "openai_key",
    "gemini_key",
    "nim_key",
}

# Everything the GUI is allowed to persist. Anything else in the payload is
# dropped, so a stray field in a request can't write arbitrary junk to disk.
ALLOWED_FIELDS = SECRET_FIELDS | {
    "url",
    "source_type",
    "mode",
    "llm_provider",
    "num_clips",
    "aspect_ratio",
    "format",
    "language",
    "openai_model",
    "gemini_model",
    "ollama_url",
    "ollama_model",
    "nim_url",
    "nim_model",
    "whisper_device",
    "whisper_model",
    "clip_length",
    # Overlay settings
    "overlay_position",
    "overlay_margin",
    "overlay_scale",
    "use_overlay_opencv",
    "overlay_enabled",
    "overlay_x",
    "overlay_y",
    "overlay_vertical_pos",
    "overlay_margin_bottom",
    "overlay_margin_left",
    # Background music bed (mixed into clips by local/music.py)
    "music_enabled",
    "music_file",
    "music_volume",
    # Post-processing toggles (silence cut + blurred bars)
    "silence_cut",
    "blur_bars",
}

MASK = "••••••••"

_lock = threading.Lock()


def load() -> Dict:
    """Read settings from disk. Returns {} if absent or corrupt."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in ALLOWED_FIELDS}


def save(incoming: Dict) -> Dict:
    """Merge `incoming` into the stored settings and write them out.

    A secret arriving as the mask placeholder means "unchanged" -- the browser
    was showing a mask, not the real key -- so the stored value is kept.
    """
    incoming = {k: v for k, v in (incoming or {}).items() if k in ALLOWED_FIELDS}

    with _lock:
        current = load()
        for key, value in incoming.items():
            if key in SECRET_FIELDS and value == MASK:
                continue  # masked placeholder: leave the stored key alone
            current[key] = value

        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_PATH)

        try:
            os.chmod(SETTINGS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass  # best-effort; Windows ACLs don't map cleanly

    return current


def mask_secrets(settings: Dict) -> Dict:
    """Replace stored secrets with a placeholder for sending to the browser.

    The GUI shows the mask so the user knows a key is saved, without the key
    itself crossing the wire again on every page load.
    """
    out = dict(settings)
    for field in SECRET_FIELDS:
        if out.get(field):
            out[field] = MASK
    return out


def resolve_secret(field: str, submitted: str) -> str:
    """Return the real key for a submitted value, expanding the mask.

    When the browser sends back the mask it means "use what you have stored".
    """
    if submitted and submitted != MASK:
        return submitted
    if submitted == MASK:
        return load().get(field, "")
    return submitted
