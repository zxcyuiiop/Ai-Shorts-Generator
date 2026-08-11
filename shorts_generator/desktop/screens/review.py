"""Review screen: approve drafts from the last run, save or discard them.

Each candidate the pipeline produced gets a card: thumbnail, title, score,
duration, a preview-on-hover (via extracted frame) and Save / Discard
buttons. Saving runs FinalizeWorker in the background so the GUI never blocks
on ffmpeg.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget, QFrame,
)

from ..finalize import FinalizeSignals, FinalizeWorker


class ClipCard(QFrame):
    """One draft candidate."""

    save_requested = Signal(object)   # emits self

    def __init__(self, short: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.short = short
        self.setObjectName("card")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._build()

    @property
    def draft_path(self) -> str:
        return str(self.short.get("clip_url") or "")

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        thumb = QLabel()
        thumb.setFixedSize(200, 120)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setObjectName("dim")
        thumb.setText("нет превью")
        self._load_thumbnail(thumb)
        lay.addWidget(thumb, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(self.short.get("title") or os.path.basename(self.draft_path))
        title.setObjectName("h2")
        title.setWordWrap(True)
        lay.addWidget(title)

        meta = []
        if self.short.get("score") is not None:
            meta.append(f"★ {self.short['score']}")
        dur = self.short.get("end_time") and self.short.get("start_time")
        if dur:
            meta.append(f"{self.short['end_time'] - self.short['start_time']:.0f} с")
        meta_lbl = QLabel("  ·  ".join(meta) if meta else "")
        meta_lbl.setObjectName("dim")
        lay.addWidget(meta_lbl)

        row = QHBoxLayout()
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(lambda: self.save_requested.emit(self))
        row.addWidget(self.save_btn)
        lay.addLayout(row)

    def _load_thumbnail(self, label: QLabel) -> None:
        """Best-effort thumbnail from the draft; silent on failure."""
        path = self.draft_path
        if not path or not os.path.isfile(path):
            return
        try:
            from shorts_generator.local.thumbgen import make_thumbnail
            thumb = make_thumbnail(path, title=False)
            if thumb and os.path.isfile(thumb):
                pix = QPixmap(thumb).scaled(
                    200, 120,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                label.setPixmap(pix)
        except Exception:
            pass


class ReviewScreen(QWidget):
    saved_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._form: Dict[str, Any] = {}
        self._shorts: List[Dict[str, Any]] = []
        self._signals: Optional[FinalizeSignals] = None
        self._worker: Optional[FinalizeWorker] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(12)

        header = QHBoxLayout()
        self.heading = QLabel("Оцените клипы")
        self.heading.setObjectName("h1")
        header.addWidget(self.heading)
        header.addStretch(1)
        self.save_all_btn = QPushButton("Сохранить все")
        self.save_all_btn.setObjectName("primary")
        self.save_all_btn.clicked.connect(self._save_all)
        header.addWidget(self.save_all_btn)
        outer.addLayout(header)

        self.status = QLabel("Сначала запустите генерацию на вкладке «Генерация».")
        self.status.setObjectName("dim")
        outer.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_host = QWidget()
        self.grid = QGridLayout(self._grid_host)
        self.grid.setSpacing(16)
        scroll.setWidget(self._grid_host)
        outer.addWidget(scroll, 1)

    # ------------------------------------------------------------- intake
    def load_result(self, form: Dict[str, Any], result: Dict[str, Any]) -> None:
        self._form = dict(form)
        self._shorts = list(result.get("shorts") or [])
        self.status.setText(
            f"Готово {len(self._shorts)} кандидатов — сохраните понравившиеся.")
        self._rebuild()

    def _rebuild(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cols = 3
        for i, short in enumerate(self._shorts):
            card = ClipCard(short)
            card.save_requested.connect(self._save_one)
            self.grid.addWidget(card, i // cols, i % cols)

    # --------------------------------------------------------------- save
    def _save_one(self, card: ClipCard) -> None:
        self._start_finalize([card.short])

    def _save_all(self) -> None:
        pending = [s for s in self._shorts if s.get("clip_url")]
        if not pending:
            return
        self._start_finalize(pending)

    def _start_finalize(self, shorts: List[Dict[str, Any]]) -> None:
        jobs = []
        for s in shorts:
            jobs.append({
                "draft_path": s.get("clip_url") or "",
                "target_aspect": s.get("target_aspect")
                                 or self._form.get("aspect_ratio") or "9:16",
                "title": s.get("title") or "",
                "source_title": self._form.get("url") or "",
                "score": s.get("score"),
                "duration_sec": (s.get("end_time") or 0) - (s.get("start_time") or 0)
                                if s.get("end_time") else None,
            })
        self.save_all_btn.setEnabled(False)
        self.status.setText("Сохранение…")

        self._signals = FinalizeSignals(self)
        self._signals.one_done.connect(self._on_one_done)
        self._signals.one_failed.connect(self._on_one_failed)
        self._signals.all_done.connect(self._on_all_done)
        self._worker = FinalizeWorker(jobs, self._form, self._signals)
        self._worker.start()

    # -------------------------------------------------------------- slots
    def _on_one_done(self, draft: str, result: Dict[str, Any]) -> None:
        self._remove_short(draft)

    def _on_one_failed(self, draft: str, error: str) -> None:
        self.status.setText(f"Ошибка сохранения: {error}")

    def _on_all_done(self, saved: int, failed: int) -> None:
        self.save_all_btn.setEnabled(True)
        msg = f"Сохранено: {saved}"
        if failed:
            msg += f", ошибок: {failed}"
        self.status.setText(msg)
        self.saved_changed.emit()
        if saved and not self._shorts:
            QMessageBox.information(self, "Готово",
                                    "Все клипы сохранены в output/saved/.")

    def _remove_short(self, draft: str) -> None:
        self._shorts = [s for s in self._shorts if s.get("clip_url") != draft]
        self._rebuild()
