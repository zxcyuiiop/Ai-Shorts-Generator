"""History screen: previously saved clips with open / favorite / delete."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from shorts_generator import history as history_store
from shorts_generator.config import LOCAL_OUTPUT_DIR


def _url_to_path(url: str) -> str:
    """'/output/saved/x/y.mp4' -> absolute filesystem path ('' when unknown)."""
    if not url or not url.startswith("/output/"):
        return ""
    rel = url[len("/output/"):].replace("/", os.sep)
    return os.path.join(os.path.realpath(LOCAL_OUTPUT_DIR), rel)


class HistoryCard(QFrame):
    """One saved clip row-card."""

    open_requested = Signal(object)    # emits self
    fav_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, clip: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.clip = clip
        self.setObjectName("card")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        thumb = QLabel()
        thumb.setFixedSize(160, 90)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setObjectName("dim")
        thumb.setText("нет превью")
        self._fill_thumb(thumb)
        lay.addWidget(thumb)

        mid = QVBoxLayout()
        title = QLabel(clip.get("title") or "(без названия)")
        title.setObjectName("h2")
        title.setWordWrap(True)
        mid.addWidget(title)

        meta: List[str] = []
        if clip.get("created_at"):
            meta.append(str(clip["created_at"]).replace("T", " "))
        if clip.get("score") is not None:
            meta.append(f"★ {clip['score']}")
        if clip.get("duration_sec") is not None:
            meta.append(f"{clip['duration_sec']:.0f} с")
        if clip.get("aspect_ratio"):
            meta.append(str(clip["aspect_ratio"]))
        meta_lbl = QLabel("  ·  ".join(meta))
        meta_lbl.setObjectName("dim")
        mid.addWidget(meta_lbl)
        mid.addStretch(1)
        lay.addLayout(mid, 1)

        self.open_btn = QPushButton("Открыть")
        self.open_btn.clicked.connect(lambda: self.open_requested.emit(self))
        lay.addWidget(self.open_btn)

        self.fav_btn = QPushButton("★" if clip.get("favorite") else "☆")
        self.fav_btn.setFixedWidth(44)
        self.fav_btn.clicked.connect(lambda: self.fav_requested.emit(self))
        lay.addWidget(self.fav_btn)

        self.del_btn = QPushButton("Удалить")
        self.del_btn.setObjectName("danger")
        self.del_btn.clicked.connect(lambda: self.delete_requested.emit(self))
        lay.addWidget(self.del_btn)

    def _fill_thumb(self, label: QLabel) -> None:
        path = _url_to_path(str(self.clip.get("thumb_url") or ""))
        if not path or not os.path.isfile(path):
            return
        pix = QPixmap(path).scaled(
            160, 90,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        label.setPixmap(pix)


class HistoryScreen(QWidget):
    """Saved clips, newest first. Reload picks up disk additions too."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._clips: List[Dict[str, Any]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(12)

        header = QHBoxLayout()
        heading = QLabel("История")
        heading.setObjectName("h1")
        header.addWidget(heading)
        header.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.reload)
        header.addWidget(refresh)
        outer.addLayout(header)

        self.status = QLabel("")
        self.status.setObjectName("dim")
        outer.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._host = QWidget()
        self._list = QVBoxLayout(self._host)
        self._list.setSpacing(10)
        self._list.addStretch(1)
        scroll.setWidget(self._host)
        outer.addWidget(scroll, 1)

    # -------------------------------------------------------------- data
    def reload(self) -> None:
        try:
            history_store.merge_disk_scan(LOCAL_OUTPUT_DIR)
        except Exception:
            pass  # scan is best-effort; the store itself is the source of truth
        self._clips = history_store.list_history()
        self._rebuild()

    def _rebuild(self) -> None:
        while self._list.count() > 1:  # keep the trailing stretch
            item = self._list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._clips:
            self.status.setText(
                "Пока пусто — сохранённые клипы появятся здесь.")
            return
        self.status.setText(f"Сохранено клипов: {len(self._clips)}")
        for i, clip in enumerate(self._clips):
            card = HistoryCard(clip)
            card.open_requested.connect(self._open)
            card.fav_requested.connect(self._toggle_fav)
            card.delete_requested.connect(self._delete)
            self._list.insertWidget(i, card)

    # ------------------------------------------------------------- actions
    def _open(self, card: HistoryCard) -> None:
        path = _url_to_path(str(card.clip.get("saved_url") or ""))
        if path and os.path.isfile(path):
            try:
                os.startfile(path)  # type: ignore[attr-defined]  (Windows)
            except OSError as e:
                self.status.setText(f"Не удалось открыть: {e}")
        else:
            self.status.setText("Файл не найден на диске.")

    def _toggle_fav(self, card: HistoryCard) -> None:
        entry = history_store.toggle_favorite(str(card.clip.get("id") or ""))
        if entry is not None:
            card.clip = entry
            card.fav_btn.setText("★" if entry.get("favorite") else "☆")

    def _delete(self, card: HistoryCard) -> None:
        cid = str(card.clip.get("id") or "")
        if history_store.delete_clip(cid):
            self._clips = [c for c in self._clips if c.get("id") != cid]
            self._rebuild()
        else:
            self.status.setText("Запись не найдена.")
