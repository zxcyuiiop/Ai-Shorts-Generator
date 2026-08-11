"""Native desktop entry point: PySide6 GUI over the existing local pipeline.

Run:  venv\\Scripts\\python.exe desktop.py
Smoke test (offscreen, quits immediately):  desktop.py --smoke
"""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from shorts_generator.desktop import theme
    from shorts_generator.desktop.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("AI Shorts Generator")
    app.setStyleSheet(theme.app_stylesheet())

    window = MainWindow()
    window.show()

    if "--smoke" in sys.argv:
        QTimer.singleShot(300, app.quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
