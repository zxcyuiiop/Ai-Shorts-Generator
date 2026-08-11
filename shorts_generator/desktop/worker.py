"""Background job workers for the desktop GUI.

Qt widgets must only be touched from the GUI thread, so the heavy pipeline
(download -> transcribe -> highlights -> clip) runs on a plain Python thread.
Two bridge objects translate that into Qt signals:

- ``PipelineSignals`` — a QObject created on the GUI thread; the worker holds a
  reference and ``emit`` calls are queued to the GUI thread automatically.
- ``LogBridge`` — replaces ``sys.stdout`` inside the worker so the pipeline's
  ``print(...)`` statements (which are the only progress channel it has) stream
  into the GUI as ``log`` signals instead of needing a parsing layer.

The worker builds a settings-override map from the persisted settings plus the
per-run form values and binds it to its own thread via config.set_overrides —
exactly what the web app did per request, so behaviour matches.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal


class LogBridge:
    """File-like object that forwards each printed chunk to a Qt signal.

    Installed as ``sys.stdout`` for the duration of the worker only. The real
    stdout is kept for anything launched with a console. Flush is a no-op that
    just emits whatever is buffered so a partial line still shows up.
    """

    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._real = sys.__stdout__
        self._buf = ""

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        # Mirror to the real console so nothing is lost when run from a terminal.
        try:
            if self._real is not None:
                self._real.write(s)
        except Exception:
            pass
        self._emit(s)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    # Some libs (tqdm, ffmpeg wrappers) probe for these.
    def isatty(self) -> bool:  # noqa: D401 - keep interface compatible
        return False


class PipelineSignals(QObject):
    """Qt signals a pipeline run can publish. Lives on the GUI thread."""

    log = Signal(str)                       # a chunk of pipeline stdout
    stage = Signal(str, int)                # (stage name, progress 0-100)
    finished = Signal(dict)                 # result dict from generate_shorts
    failed = Signal(str)                    # human-readable error


# Recognised stage markers in the pipeline's stdout -> (label, rough progress).
_STAGE_MARKERS = [
    ("[download", "Скачивание", 15),
    ("[transcribe", "Транскрибация", 35),
    ("[highlights]", "Поиск хайлайтов", 55),
    ("cropping", "Монтаж клипов", 70),
    ("[clip/local]", "Монтаж клипов", 75),
]


def _apply_overrides(form: Dict[str, Any]) -> None:
    """Bind this run's settings to the current (worker) thread.

    form carries GUI field names; merge over the persisted settings store so a
    desktop run honours saved provider keys / models exactly like the web app's
    /api/generate did.
    """
    from shorts_generator.config import set_overrides
    from shorts_generator import settings_store

    merged: Dict[str, Any] = dict(settings_store.load())
    merged.update({k: v for k, v in form.items() if v not in (None, "")})

    # Lower-case GUI field -> the env-style keys config.env() reads. Mirror the
    # aliases the web layer supplied so feature toggles actually take effect.
    mapping: Dict[str, str] = {
        "muapi_key": "MUAPI_API_KEY",
        "openai_key": "OPENAI_API_KEY",
        "gemini_key": "GEMINI_API_KEY",
        "nim_key": "NIM_API_KEY",
        "nim_url": "NIM_BASE_URL",
        "nim_model": "NIM_MODEL",
        "openai_model": "OPENAI_MODEL",
        "gemini_model": "GEMINI_MODEL",
        "ollama_url": "OLLAMA_BASE_URL",
        "ollama_model": "OLLAMA_MODEL",
        "whisper_device": "LOCAL_WHISPER_DEVICE",
        "whisper_model": "LOCAL_WHISPER_MODEL",
        "overlay_enabled": "OVERLAY_ENABLED",
        "overlay_x": "OVERLAY_X",
        "overlay_y": "OVERLAY_Y",
        "overlay_scale": "OVERLAY_SCALE",
        "overlay_margin": "OVERLAY_MARGIN",
        "blur_bars": "BLUR_BARS",
        "music_enabled": "MUSIC_ENABLED",
        "music_file": "MUSIC_FILE",
        "music_volume": "MUSIC_VOLUME",
        "silence_cut": "SILENCE_CUT",
        "captions_enabled": "CAPTIONS_ENABLED",
        "caption_position": "CAPTION_POSITION",
        "caption_margin_v": "CAPTION_MARGIN_V",
        "caption_style": "CAPTION_STYLE",
        "face_track": "FACE_TRACK_ENABLED",
        "title_enabled": "TITLE_ENABLED",
        "title_y_from_bottom": "TITLE_Y_FROM_BOTTOM",
        "title_font_size": "TITLE_FONT_SIZE",
        "watermark_enabled": "WATERMARK_ENABLED",
        "watermark_file": "WATERMARK_FILE",
        "watermark_at_sec": "WATERMARK_AT_SEC",
        "watermark_duration_sec": "WATERMARK_DURATION_SEC",
        "watermark_scale": "WATERMARK_SCALE",
    }
    overrides: Dict[str, Any] = {}
    for field, env_name in mapping.items():
        if field in merged:
            val = merged[field]
            if isinstance(val, bool):
                val = "1" if val else "0"
            overrides[env_name] = val
        # Also accept already-uppercase keys straight from the store.
        if env_name in merged:
            overrides[env_name] = merged[env_name]
    set_overrides(overrides)


class PipelineWorker(threading.Thread):
    """Runs generate_shorts off the GUI thread and reports back via signals."""

    def __init__(self, form: Dict[str, Any], signals: PipelineSignals):
        super().__init__(daemon=True, name="pipeline-worker")
        self._form = dict(form)
        self.signals = signals

    # -- helpers -----------------------------------------------------
    def _emit_stage_for_line(self, line: str) -> None:
        low = line.lower()
        for marker, label, pct in _STAGE_MARKERS:
            if marker.lower() in low:
                self.signals.stage.emit(label, pct)
                return

    def _log(self, chunk: str) -> None:
        self.signals.log.emit(chunk)
        for line in chunk.splitlines():
            self._emit_stage_for_line(line)

    # -- thread body -------------------------------------------------
    def run(self) -> None:  # noqa: D401 - thread entry point
        from shorts_generator.config import clear_overrides
        from shorts_generator import generate_shorts

        old_stdout = sys.stdout
        bridge = LogBridge(self._log)
        sys.stdout = bridge
        try:
            _apply_overrides(self._form)
            self.signals.stage.emit("Запуск", 5)
            result = generate_shorts(
                youtube_url=self._form.get("url", ""),
                num_clips=int(self._form.get("num_clips") or 3),
                aspect_ratio=self._form.get("aspect_ratio") or "9:16",
                download_format=str(self._form.get("format") or "720"),
                language=self._form.get("language") or None,
                mode=(self._form.get("mode") or "local"),
                llm_provider=self._form.get("llm_provider") or None,
                clip_length=self._form.get("clip_length") or None,
            )
        except Exception as e:  # noqa: BLE001 - surface anything to the GUI
            traceback.print_exc()
            self.signals.failed.emit(str(e))
        else:
            self.signals.stage.emit("Готово", 100)
            self.signals.finished.emit(result)
        finally:
            sys.stdout = old_stdout
            try:
                clear_overrides()
            except Exception:
                pass
