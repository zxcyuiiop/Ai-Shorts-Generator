"""Desktop-side "save a draft" = the web app's /api/shorts/save without Flask.

The pipeline leaves each approved-looking candidate as a *draft* (16:9, no
effects). Finalizing one means: reframe to the target aspect, then run
``finalize_clip_local`` which burns blur bars / captions / title / watermark /
overlay / music per the current overrides, and move the result into
``output/saved/`` + record it in the history store.

Runs on its own thread so the GUI never blocks; config overrides are
thread-local, so they are bound inside this thread (not the GUI thread).
"""
from __future__ import annotations

import os
import shutil
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal


class FinalizeSignals(QObject):
    """Progress for a batch finalize. Lives on the GUI thread."""

    log = Signal(str)
    one_done = Signal(str, dict)   # (draft_path, result)
    one_failed = Signal(str, str)  # (draft_path, error)
    all_done = Signal(int, int)    # (saved_count, failed_count)


def finalize_draft(
    draft_path: str,
    target_aspect: str,
    title: str = "",
    source_title: str = "",
    delete_source: bool = True,
) -> Dict[str, Any]:
    """Reframe + burn effects + move into ``output/saved/``.

    Synchronous; call from a worker thread. ``config.set_overrides`` must have
    been bound on *this* thread beforehand (FinalizeWorker does it). Returns a
    result dict with ``final_path`` / ``saved_url`` / ``thumb_url``.

    Raises on a hard failure and leaves the draft in place.
    """
    from shorts_generator.config import LOCAL_OUTPUT_DIR
    from shorts_generator.local.blurpad import blurpad_enabled_for
    from shorts_generator.local.clipper import _reframe_vertical, finalize_clip_local

    if not os.path.isfile(draft_path):
        raise FileNotFoundError(f"draft not found: {draft_path}")

    tmp = draft_path + ".tmp_save.mp4"
    captions_ass = draft_path + ".ass"
    captions_ass = captions_ass if os.path.isfile(captions_ass) else None

    # Web parity: for 9:16 with blur bars the prerender itself becomes vertical
    # (the blurred pillarbox needs the FULL frame, not a centre crop), so we
    # copy the draft and let finalize's blurpad stage build the vertical frame.
    if target_aspect == "9:16" and blurpad_enabled_for("9:16"):
        shutil.copy2(draft_path, tmp)
    else:
        _reframe_vertical(draft_path, tmp, target_aspect)

    finalize_clip_local(tmp, target_aspect, captions_ass=captions_ass,
                        title_text=(title or ""))

    # Move into saved/ keeping the draft's player subfolder layout.
    output_dir = os.path.realpath(LOCAL_OUTPUT_DIR)
    draft_real = os.path.realpath(draft_path)
    rel = os.path.relpath(draft_real, output_dir).replace("\\", "/")
    subdir = os.path.dirname(rel)
    saved_dir = os.path.join(output_dir, "saved", subdir)
    os.makedirs(saved_dir, exist_ok=True)

    final_name = os.path.basename(draft_path)
    final_path = os.path.join(saved_dir, final_name)
    n = 1
    stem, ext = os.path.splitext(final_name)
    while os.path.exists(final_path):
        n += 1
        final_name = f"{stem}_{n}{ext}"
        final_path = os.path.join(saved_dir, final_name)

    shutil.move(tmp, final_path)
    if delete_source:
        try:
            os.remove(draft_real)
        except OSError:
            pass
    # The caption sidecar follows its clip into saved/ (review tooling may
    # still want it); a burn already consumed the text, this is for parity.
    if captions_ass:
        try:
            shutil.move(captions_ass, final_path + ".ass")
        except OSError:
            pass

    rel_saved = os.path.relpath(final_path, output_dir).replace("\\", "/")
    saved_url = f"/output/{rel_saved}"

    thumb_url = None
    try:
        from shorts_generator.local.thumbgen import make_thumbnail
        thumb_path = make_thumbnail(final_path, title=False)
        if thumb_path:
            rel_thumb = os.path.relpath(thumb_path, output_dir).replace("\\", "/")
            thumb_url = f"/output/{rel_thumb}"
    except Exception as e:  # thumbnail is nice-to-have; never fail the save
        print(f"[desktop/save] thumbnail failed for {final_name}: {e}", flush=True)

    return {
        "draft": draft_path,
        "final_path": final_path,
        "saved_url": saved_url,
        "thumb_url": thumb_url,
        "aspect_ratio": target_aspect,
        "name": os.path.splitext(final_name)[0],
    }


class FinalizeWorker(threading.Thread):
    """Finalize a list of drafts sequentially, emitting per-item signals."""

    def __init__(self, jobs: List[Dict[str, Any]], form: Dict[str, Any],
                 signals: FinalizeSignals):
        """jobs: one dict per draft with keys draft_path/target_aspect/title/
        source_title. form: the run's form dict for _apply_overrides."""
        super().__init__(daemon=True, name="finalize-worker")
        self._jobs = [dict(j) for j in jobs]
        self._form = dict(form)
        self.signals = signals

    def run(self) -> None:  # noqa: D401 - thread entry point
        from shorts_generator.config import clear_overrides
        from shorts_generator import history
        from .worker import LogBridge, _apply_overrides

        import sys
        old_stdout = sys.stdout
        sys.stdout = LogBridge(lambda s: self.signals.log.emit(s))

        saved = 0
        failed = 0
        try:
            _apply_overrides(self._form)
            for job in self._jobs:
                draft = job.get("draft_path") or ""
                try:
                    result = finalize_draft(
                        draft,
                        target_aspect=job.get("target_aspect") or "9:16",
                        title=job.get("title") or "",
                        source_title=job.get("source_title") or "",
                    )
                    _record_history(result, job, history)
                    saved += 1
                    self.signals.one_done.emit(draft, result)
                except Exception as e:  # keep going; one bad clip != all lost
                    traceback.print_exc()
                    failed += 1
                    self.signals.one_failed.emit(draft, str(e))
        finally:
            sys.stdout = old_stdout
            try:
                clear_overrides()
            except Exception:
                pass
        self.signals.all_done.emit(saved, failed)


def _record_history(result: Dict[str, Any], job: Dict[str, Any], history) -> None:
    """Best-effort history entry; mirrors the web's _history_for_saved_clip."""
    try:
        history.add_clip(
            title=job.get("title") or result.get("name") or "",
            source_title=job.get("source_title") or "",
            saved_url=result.get("saved_url") or "",
            thumb_url=result.get("thumb_url"),
            score=job.get("score"),
            duration_sec=job.get("duration_sec"),
            aspect_ratio=result.get("aspect_ratio") or "",
        )
    except Exception as e:  # history is best-effort, never lose the save
        print(f"[desktop/save] history add failed: {e}", flush=True)
