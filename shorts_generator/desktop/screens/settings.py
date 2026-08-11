"""Settings screen: API keys, models and effect parameters.

Reads/writes ``settings_store`` exactly like the web settings page. Secret
fields show a mask when a value is stored; submitting the mask back keeps the
stored key, submitting a new value replaces it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from shorts_generator import settings_store


class SettingsScreen(QWidget):
    """Emitted after settings were persisted, so other screens can refresh."""

    saved = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._secret_masked: Dict[str, bool] = {}
        self._build()
        self.reload()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)

        title = QLabel("Настройки")
        title.setObjectName("h1")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setSpacing(14)

        # --- API keys -------------------------------------------------
        keys = QGroupBox("API-ключи")
        kf = QFormLayout(keys)
        self.key_edits: Dict[str, QLineEdit] = {}
        for field, label in (("openai_key", "OpenAI API key"),
                             ("gemini_key", "Gemini API key"),
                             ("nim_key", "NVIDIA NIM key"),
                             ("muapi_key", "MuAPI key")):
            e = QLineEdit()
            e.setEchoMode(QLineEdit.EchoMode.Password)
            e.setPlaceholderText("sk-…")
            self.key_edits[field] = e
            kf.addRow(label, e)
        lay.addWidget(keys)

        # --- LLM / whisper ---------------------------------------------
        llm = QGroupBox("Модели")
        lf = QFormLayout(llm)
        self.llm_provider = QComboBox()
        for label, val in (("Авто", ""), ("OpenAI", "openai"), ("Gemini", "gemini"),
                           ("Ollama", "ollama"), ("NVIDIA NIM", "nim")):
            self.llm_provider.addItem(label, val)
        lf.addRow("LLM-провайдер", self.llm_provider)
        self.openai_model = QLineEdit()
        self.openai_model.setPlaceholderText("gpt-4o-mini")
        lf.addRow("OpenAI модель", self.openai_model)
        self.gemini_model = QLineEdit()
        self.gemini_model.setPlaceholderText("gemini-1.5-flash")
        lf.addRow("Gemini модель", self.gemini_model)
        self.whisper_device = QComboBox()
        for label, val in (("Авто", ""), ("GPU (cuda)", "cuda"), ("CPU", "cpu")):
            self.whisper_device.addItem(label, val)
        lf.addRow("Whisper устройство", self.whisper_device)
        self.whisper_model = QComboBox()
        for m in ("tiny", "base", "small", "medium", "large-v2"):
            self.whisper_model.addItem(m, m)
        lf.addRow("Whisper модель", self.whisper_model)
        lay.addWidget(llm)

        # --- captions ----------------------------------------------------
        cap = QGroupBox("Субтитры")
        cf = QFormLayout(cap)
        self.caption_style = QComboBox()
        for label, val in (("Караоке", "karaoke"), ("Классика", "classic")):
            self.caption_style.addItem(label, val)
        cf.addRow("Стиль", self.caption_style)
        self.caption_position = QComboBox()
        for label, val in (("Снизу", "bottom"), ("Центр", "center"), ("Сверху", "top")):
            self.caption_position.addItem(label, val)
        cf.addRow("Позиция", self.caption_position)
        self.caption_margin_v = QSpinBox()
        self.caption_margin_v.setRange(0, 1200)
        self.caption_margin_v.setValue(150)
        cf.addRow("Отступ от края, px", self.caption_margin_v)
        lay.addWidget(cap)

        # --- title --------------------------------------------------------
        ttl = QGroupBox("Заголовок на видео")
        tf = QFormLayout(ttl)
        self.title_y_from_bottom = QSpinBox()
        self.title_y_from_bottom.setRange(100, 1500)
        self.title_y_from_bottom.setValue(750)
        tf.addRow("Отступ снизу, px", self.title_y_from_bottom)
        self.title_font_size = QSpinBox()
        self.title_font_size.setRange(24, 200)
        self.title_font_size.setValue(64)
        tf.addRow("Размер шрифта", self.title_font_size)
        lay.addWidget(ttl)

        # --- music ----------------------------------------------------------
        mus = QGroupBox("Музыка")
        mf = QFormLayout(mus)
        mus_row = QHBoxLayout()
        self.music_file = QLineEdit()
        self.music_file.setPlaceholderText("Путь к mp3…")
        mus_btn = QPushButton("Обзор…")
        mus_btn.clicked.connect(self._browse_music)
        mus_row.addWidget(self.music_file, 1)
        mus_row.addWidget(mus_btn)
        mus_host = QWidget()
        mus_host.setLayout(mus_row)
        mf.addRow("Файл", mus_host)
        self.music_volume = QDoubleSpinBox()
        self.music_volume.setRange(0.0, 2.0)
        self.music_volume.setSingleStep(0.05)
        self.music_volume.setValue(0.15)
        mf.addRow("Громкость", self.music_volume)
        lay.addWidget(mus)

        # --- watermark -----------------------------------------------------
        wm = QGroupBox("Водяной знак (банер-вставка)")
        wf = QFormLayout(wm)
        wm_row = QHBoxLayout()
        self.watermark_file = QLineEdit()
        self.watermark_file.setPlaceholderText("Путь к .mov/.png…")
        wm_btn = QPushButton("Обзор…")
        wm_btn.clicked.connect(self._browse_watermark)
        wm_row.addWidget(self.watermark_file, 1)
        wm_row.addWidget(wm_btn)
        wm_host = QWidget()
        wm_host.setLayout(wm_row)
        wf.addRow("Файл", wm_host)
        self.watermark_at_sec = QDoubleSpinBox()
        self.watermark_at_sec.setRange(0.0, 600.0)
        self.watermark_at_sec.setSingleStep(0.1)
        self.watermark_at_sec.setValue(2.0)
        wf.addRow("Показать на, сек", self.watermark_at_sec)
        lay.addWidget(wm)

        lay.addStretch(1)
        outer.addWidget(scroll, 1)

        # --- actions -------------------------------------------------------
        btns = QHBoxLayout()
        btns.addStretch(1)
        save_btn = QPushButton("Сохранить настройки")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save)
        btns.addWidget(save_btn)
        outer.addLayout(btns)

    # ---------------------------------------------------------------- io
    def _browse_music(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "Музыкальный файл", "", "Аудио (*.mp3 *.wav *.m4a);;Все файлы (*)")
        if p:
            self.music_file.setText(p)

    def _browse_watermark(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "Файл водяного знака", "",
            "Видео/картинка (*.mov *.mp4 *.png *.jpg);;Все файлы (*)")
        if p:
            self.watermark_file.setText(p)

    def reload(self) -> None:
        """Pull persisted values into the widgets (secrets stay masked)."""
        s = settings_store.load()
        for field, edit in self.key_edits.items():
            stored = s.get(field, "")
            if stored:
                edit.setText(settings_store.MASK)
                self._secret_masked[field] = True
            else:
                edit.setText("")
                self._secret_masked[field] = False
        _set_combo(self.llm_provider, s.get("llm_provider"))
        self.openai_model.setText(str(s.get("openai_model") or ""))
        self.gemini_model.setText(str(s.get("gemini_model") or ""))
        _set_combo(self.whisper_device, s.get("whisper_device"))
        _set_combo(self.whisper_model, s.get("whisper_model"))
        _set_combo(self.caption_style, s.get("caption_style"))
        _set_combo(self.caption_position, s.get("caption_position"))
        self.caption_margin_v.setValue(_int(s.get("caption_margin_v"), 150))
        self.title_y_from_bottom.setValue(_int(s.get("title_y_from_bottom"), 750))
        self.title_font_size.setValue(_int(s.get("title_font_size"), 64))
        self.music_file.setText(str(s.get("music_file") or ""))
        self.music_volume.setValue(_float(s.get("music_volume"), 0.15))
        self.watermark_file.setText(str(s.get("watermark_file") or ""))
        self.watermark_at_sec.setValue(_float(s.get("watermark_at_sec"), 2.0))

    def _save(self) -> None:
        payload: Dict[str, Any] = {}
        for field, edit in self.key_edits.items():
            text = edit.text().strip()
            if text == settings_store.MASK:
                continue   # unchanged — keep the stored key
            payload[field] = text
        payload.update({
            "llm_provider": self.llm_provider.currentData() or "",
            "openai_model": self.openai_model.text().strip(),
            "gemini_model": self.gemini_model.text().strip(),
            "whisper_device": self.whisper_device.currentData() or "",
            "whisper_model": self.whisper_model.currentData() or "",
            "caption_style": self.caption_style.currentData() or "karaoke",
            "caption_position": self.caption_position.currentData() or "bottom",
            "caption_margin_v": self.caption_margin_v.value(),
            "title_y_from_bottom": self.title_y_from_bottom.value(),
            "title_font_size": self.title_font_size.value(),
            "music_file": self.music_file.text().strip(),
            "music_volume": self.music_volume.value(),
            "watermark_file": self.watermark_file.text().strip(),
            "watermark_at_sec": self.watermark_at_sec.value(),
        })
        try:
            settings_store.save(payload)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить: {e}")
            return
        QMessageBox.information(self, "Сохранено", "Настройки сохранены.")
        self.reload()
        self.saved.emit()


def _set_combo(combo: QComboBox, value) -> None:
    if value in (None, ""):
        return
    idx = combo.findData(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
