"""Persistent history of saved clips, stored as JSON next to the output dir.

Every draft approved through the save flow (``_do_save``) lands here with its
title, source title, score/duration from the originating job, and a thumbnail
URL (thumbnails live in ``output/thumbs/``). The file is
``output/history.json`` -- gitignored by location, same idea as
settings.local.json.

Concurrency: one module-level lock guards read-modify-write cycles; writes are
atomic via ``<file>.tmp`` + ``os.replace``. Loading is tolerant: a missing or
corrupt file yields an empty store and a log line instead of an exception.

Tests redirect the storage location by monkeypatching ``HISTORY_FILE`` before
use (direct assignment, like settings_store.SETTINGS_PATH), or by setting the
``HISTORY_PATH`` env var *before* importing this module.
"""
import json
import os
import threading
import time
from typing import Dict, List, Optional

# Read once at import so tests can either set the env var first or simply
# monkeypatch this attribute afterwards (mirrors settings_store.SETTINGS_PATH).
HISTORY_FILE = (
    os.getenv("HISTORY_PATH")
    or os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir,
            "output", "history.json",
        )
    )
)

_lock = threading.Lock()


def _load() -> Dict:
    """Read the store from disk. Corrupt/missing -> empty store + log."""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"clips": []}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"[history] WARNING: could not read {HISTORY_FILE}: {e} -- "
              f"starting with an empty store.", flush=True)
        return {"clips": []}
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        print(f"[history] WARNING: {HISTORY_FILE} has an unexpected shape -- "
              f"starting with an empty store.", flush=True)
        return {"clips": []}
    return data


def _save(store: Dict) -> None:
    """Persist the store atomically (<file>.tmp + os.replace)."""
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)


def _new_id(clips: List[Dict]) -> str:
    """``h<ms>_<counter>`` -- time-ordered-ish, collision-free per this store."""
    ms = int(time.time() * 1000)
    n = 0
    existing = {c.get("id") for c in clips}
    while f"h{ms}_{n}" in existing:
        n += 1
    return f"h{ms}_{n}"


def _coerce_number(value) -> Optional[float]:
    """int/float pass through; numeric strings convert; anything else -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _find(clips: List[Dict], clip_id: str) -> Optional[Dict]:
    for clip in clips:
        if clip.get("id") == clip_id:
            return clip
    return None


def add_clip(**fields) -> Dict:
    """Append one entry and persist it. Returns the stored entry.

    Recognized fields: title, source_title, saved_url, thumb_url, score,
    duration_sec, aspect_ratio, created_at (defaults: now, ISO local time),
    favorite (defaults False). Unknown fields are dropped.
    """
    with _lock:
        store = _load()
        entry: Dict = {
            "id": _new_id(store["clips"]),
            "title": str(fields.get("title") or ""),
            "source_title": str(fields.get("source_title") or ""),
            "saved_url": str(fields.get("saved_url") or ""),
            "thumb_url": fields.get("thumb_url") or None,
            "score": _coerce_number(fields.get("score")),
            "duration_sec": _coerce_number(fields.get("duration_sec")),
            "aspect_ratio": str(fields.get("aspect_ratio") or ""),
            "created_at": str(fields.get("created_at")
                              or time.strftime("%Y-%m-%dT%H:%M:%S")),
            "favorite": bool(fields.get("favorite", False)),
        }
        store["clips"].append(entry)
        _save(store)
    return entry


def list_history(limit: int = 500) -> List[Dict]:
    """Newest first (storage is oldest-first), capped at ``limit`` entries."""
    with _lock:
        clips = list(_load()["clips"])
    clips.reverse()
    return clips[:limit]


def delete_clip(clip_id: str) -> bool:
    """Remove an entry by id. True when one was actually removed."""
    with _lock:
        store = _load()
        entry = _find(store["clips"], clip_id)
        if entry is None:
            return False
        store["clips"].remove(entry)
        _save(store)
    return True


def toggle_favorite(clip_id: str) -> Optional[Dict]:
    """Flip ``favorite`` on an entry and return it; None when not found."""
    with _lock:
        store = _load()
        entry = _find(store["clips"], clip_id)
        if entry is None:
            return None
        entry["favorite"] = not bool(entry.get("favorite"))
        _save(store)
        return dict(entry)


def merge_disk_scan(output_dir: str) -> int:
    """Backfill entries for saved clips that predate this history store.

    Walks ``<output_dir>/saved/**/*.mp4``; every file whose ``/output/...`` URL
    is not yet tracked gets an entry titled from its filename, created_at from
    the file mtime, no score and no thumbnail. Returns how many were added.
    """
    saved_dir = os.path.realpath(os.path.join(output_dir, "saved"))
    if not os.path.isdir(saved_dir):
        return 0
    output_dir = os.path.realpath(output_dir)
    with _lock:
        store = _load()
        known = {c.get("saved_url") for c in store["clips"]}
        added = 0
        for root, _dirs, files in os.walk(saved_dir):
            for name in sorted(files):
                if not name.lower().endswith(".mp4"):
                    continue
                abs_path = os.path.realpath(os.path.join(root, name))
                rel = os.path.relpath(abs_path, output_dir).replace("\\", "/")
                url = f"/output/{rel}"
                if url in known:
                    continue
                try:
                    mtime = os.path.getmtime(abs_path)
                except OSError:
                    continue
                store["clips"].append({
                    "id": _new_id(store["clips"]),
                    "title": os.path.splitext(name)[0],
                    "source_title": "",
                    "saved_url": url,
                    "thumb_url": None,
                    "score": None,
                    "duration_sec": None,
                    "aspect_ratio": "",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                time.localtime(mtime)),
                    "favorite": False,
                })
                known.add(url)
                added += 1
        if added:
            _save(store)
    return added
