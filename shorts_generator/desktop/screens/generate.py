"""Generate screen: source (YouTube URL or local file) + options + run log.

Builds a plain ``form`` dict from the widgets, hands it to PipelineWorker, and
streams the pipeline's stdout into a read-only log pane. When the run
finishes, the result dict is handed upward via ``run_finished`` so the main
window can show the review screen.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from ..worker import PipelineSignals, PipelineWorker


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    lay = QHBoxLayout()
    lab = QLabel(label)
    lab.setMinimumWidth(150)
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    return lay


class GenerateScreen(QWidget):
    run_started = Signal()
    run_finished = Signal(dict, dict)   # (form, result)
    run_failed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._signals: Optional[PipelineSignals] = None
        self._worker: Optional[PipelineWorker] = None
        self._build()
        self._load_defaults()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("Сгенерировать шортсы")
        title.setObjectName("h1")
        root.addWidget(title)
        sub = QLabel("YouTube-ссылка или локальный файл → нарезka вертикальных клипов")
        sub.setObjectName("dim")
        root.addWidget(sub)

        # --- source group ---------------------------------------------
        src = QGroupBox("Источник видео")
        src_l = QVBoxLayout(src)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://youtube.com/watch?v=…")
        src_l.addLayout(_row("Ссылка YouTube", self.url_edit))

        file_row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("…или выберите локальный видеофайл")
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(browse)
        lab = QLabel("Локальный файл")
        lab.setMinimumWidth(150)
        wrap = QHBoxLayout()
        wrap.addWidget(lab)
        wrap.addLayout(file_row, 1)
        src_l.addLayout(wrap)

        hint = QLabel("Если заполнены оба поля — используется локальный файл.")
        hint.setObjectName("dim")
        src_l.addWidget(hint)
        root.addWidget(src)

        # --- options group --------------------------------------------
        opts = QGroupBox("Параметры")
        grid = QGridLayout(opts)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)

        self.num_clips = QSpinBox()
        self.num_clips.setRange(1, 20)
        self.num_clips.setValue(3)

        self.clip_length = QComboBox()
        self.clip_length.addItem("Любая длина", "any")
        self.clip_length.addItem("Короткие (<30 с)", "short")
        self.clip_length.addItem("Средние (30–60 с)", "medium")
        self.clip_length.addItem("Длинные (60–90 с)", "long")
        self.clip_length.addItem("Расширенные (90–180 с)", "extended")

        self.aspect = QComboBox()
        for label, val in (("9:16 — вертикаль", "9:16"),
                           ("1:1 — квадрат", "1:1"),
                           ("16:9 — горизонталь", "16:9")):
            self.aspect.addItem(label, val)

        self.fmt = QComboBox()
        for f in ("360", "480", "720", "1080"):
            self.fmt.addItem(f"{f}p", f)
        self.fmt.setCurrentIndex(2)

        self.language = QComboBox()
        self.language.addItem("Авто", "")
        self.language.addItem("Русский", "ru")
        self.language.addItem("English", "en")

        grid.addWidget(QLabel("Клипов"), 0, 0)
        grid.addWidget(self.num_clips, 0, 1)
        grid.addWidget(QLabel("Длина"), 0, 2)
        grid.addWidget(self.clip_length, 0, 3)
        grid.addWidget(QLabel("Формат кадра"), 1, 0)
        grid.addWidget(self.aspect, 1, 1)
        grid.addWidget(QLabel("Качество"), 1, 2)
        grid.addWidget(self.fmt, 1, 3)
        grid.addWidget(QLabel("Язык речи"), 2, 0)
        grid.addWidget(self.language, 2, 1)
        root.addWidget(opts)

        # --- effects toggle (quick switches; detail lives in Настройки) --
        fx = QGroupBox("Эффекты")
        fx_l = QHBoxLayout(fx)
        self.fx_captions = QCheckBox("Субтитры")
        self.fx_captions.setChecked(True)
        self.fx_blur = QCheckBox("Размытые полосы")
        self.fx_blur.setChecked(True)
        self.fx_silence = QCheckBox("Вырезать паузы")
        self.fx_silence.setChecked(True)
        self.fx_music = QCheckBox("Музыка")
        self.fx_title = QCheckBox("Заголовок")
        self.fx_watermark = QCheckBox("Водяной знак")
        for cb in (self.fx_captions, self.fx_blur, self.fx_silence,
                   self.fx_music, self.fx_title, self.fx_watermark):
            fx_l.addWidget(cb)
        fx_l.addStretch(1)
        root.addWidget(fx)

        # --- run bar -----------------------------------------------------
        run_row = QHBoxLayout()
        self.run_btn = QPushButton("Сгенерировать")
        self.run_btn.setObjectName("primary")
        self.run_btn.setMinimumHeight(40)
        self.run_btn.clicked.connect(self._start)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.stage_label = QLabel("")
        self.stage_label.setObjectName("dim")
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.progress, 1)
        run_row.addWidget(self.stage_label)
        root.addLayout(run_row)

        # --- log ----------------------------------------------------------
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("mono")
        self.log.setPlaceholderText("Здесь появится ход выполнения…")
        root.addWidget(self.log, 1)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите видеофайл", "",
            "Видео (*.mp4 *.mov *.mkv *.webm *.avi);;Все файлы (*)")
        if path:
            self.file_edit.setText(path)

    # ------------------------------------------------------------ settings
    def reload_defaults(self) -> None:
        """Public wrapper so the main window can refresh after Settings save."""
        self._load_defaults()

    def _load_defaults(self) -> None:
        from shorts_generator import settings_store
        s = settings_store.load()
        # Restore the last-used run options so a reopen feels continuous.
        if s.get("url"):
            self.url_edit.setText(str(s["url"]))
        self.num_clips.setValue(int(s.get("num_clips") or 3))
        _set_combo(self.aspect, s.get("aspect_ratio"))
        _set_combo(self.clip_length, s.get("clip_length"))
        _set_combo(self.fmt, s.get("format"))
        _set_combo(self.language, s.get("language"))
        for cb, key in ((self.fx_captions, "captions_enabled"),
                        (self.fx_blur, "blur_bars"),
                        (self.fx_silence, "silence_cut"),
                        (self.fx_music, "music_enabled"),
                        (self.fx_title, "title_enabled"),
                        (self.fx_watermark, "watermark_enabled")):
            if key in s:
                cb.setChecked(_truthy(s[key]))

    # ---------------------------------------------------------------- run
    def form(self) -> Dict[str, Any]:
        """Current widget state as the flat dict the worker expects."""
        file_path = self.file_edit.text().strip()
        url = self.url_edit.text().strip()
        source = file_path if file_path else url
        return {
            "url": source,
            "source_type": "file" if file_path else "url",
            "mode": "local",                     # desktop is always local mode
            "num_clips": self.num_clips.value(),
            "clip_length": self.clip_length.currentData() or "any",
            "aspect_ratio": self.aspect.currentData() or "9:16",
            "format": self.fmt.currentData() or "720",
            "language": self.language.currentData() or None,
            "captions_enabled": self.fx_captions.isChecked(),
            "blur_bars": self.fx_blur.isChecked(),
            "silence_cut": self.fx_silence.isChecked(),
            "music_enabled": self.fx_music.isChecked(),
            "title_enabled": self.fx_title.isChecked(),
            "watermark_enabled": self.fx_watermark.isChecked(),
        }

    def _start(self) -> None:
        form = self.form()
        if not form["url"]:
            self.log.appendPlainText("⚠ Укажите YouTube-ссылку или выберите файл.")
            return
        # Persist run options so the next launch remembers them.
        try:
            from shorts_generator import settings_store
            settings_store.save({
                "url": form["url"] if form["source_type"] == "url" else "",
                "num_clips": form["num_clips"],
                "aspect_ratio": form["aspect_ratio"],
                "clip_length": form["clip_length"],
                "format": form["format"],
                "language": form["language"] or "",
                "captions_enabled": form["captions_enabled"],
                "blur_bars": form["blur_bars"],
                "silence_cut": form["silence_cut"],
                "music_enabled": form["music_enabled"],
                "title_enabled": form["title_enabled"],
                "watermark_enabled": form["watermark_enabled"],
            })
        except Exception:
            pass

        self.run_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.log.clear()
        self.run_started.emit()

        self._signals = PipelineSignals(self)
        self._signals.log.connect(self._on_log)
        self._signals.stage.connect(self._on_stage)
        self._signals.finished.connect(lambda res: self._on_done(form, res))
        self._signals.failed.connect(self._on_failed)

        self._worker = PipelineWorker(form, self._signals)
        self._worker.start()

    # -------------------------------------------------------------- slots
    def _on_log(self, chunk: str) -> None:
        cursor = self.log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log.setTextCursor(cursor)
        self.log.insertPlainText(chunk)
        self.log.ensureCursorVisible()

    def _on_stage(self, label: str, pct: int) -> None:
        self.stage_label.setText(label)
        self.progress.setValue(max(self.progress.value(), pct))

    def _on_done(self, form: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.run_btn.setEnabled(True)
        self.stage_label.setText("Готово")
        self.progress.setValue(100)
        self.run_finished.emit(form, result)

    def _on_failed(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.stage_label.setText("Ошибка")
        self.log.appendPlainText(f"\n✖ Ошибка: {message}")
        self.run_failed.emit(message)


def _set_combo(combo: QComboBox, value) -> None:
    if value in (None, ""):
        return
    idx = combo.findData(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes")
