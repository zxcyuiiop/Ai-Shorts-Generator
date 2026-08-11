"""Main window: left navigation + stacked screens (generate/review/history/settings)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget,
    QWidget,
)

from .screens.generate import GenerateScreen
from .screens.history import HistoryScreen
from .screens.review import ReviewScreen
from .screens.settings import SettingsScreen


class MainWindow(QMainWindow):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("AI Shorts Generator")
        self.resize(1200, 800)

        central = QWidget()
        central.setObjectName("root")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(200)
        self.nav.setSpacing(2)
        for label in ("Генерация", "Ревью", "История", "Настройки"):
            QListWidgetItem(label, self.nav)
        self.nav.setCurrentRow(0)
        root.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.generate = GenerateScreen()
        self.review = ReviewScreen()
        self.history = HistoryScreen()
        self.settings = SettingsScreen()
        for screen in (self.generate, self.review, self.history, self.settings):
            self.stack.addWidget(screen)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.nav.currentRowChanged.connect(self._on_nav)

        self.generate.run_finished.connect(self._on_run_finished)
        self.review.saved_changed.connect(self.history.reload)
        # New defaults / keys changed in Settings should apply to the next run.
        self.settings.saved.connect(self.generate.reload_defaults)

    # ------------------------------------------------------------------ nav
    def _on_nav(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        if self.stack.currentWidget() is self.history:
            self.history.reload()
        elif self.stack.currentWidget() is self.settings:
            self.settings.reload()

    # ---------------------------------------------------------------- wiring
    def _on_run_finished(self, form: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.review.load_result(form, result)
        self.stack.setCurrentWidget(self.review)
        self.nav.setCurrentRow(1)
