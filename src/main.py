"""Điểm khởi động ứng dụng LiveYoutube.

Chạy:  python -m src.main    (từ thư mục gốc dự án)
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LiveYoutube")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
