"""Entry point cho bản đóng gói (PyInstaller).

Import package `src` để các import tương đối trong src.* hoạt động,
khác với việc chạy trực tiếp src/main.py.
"""
import sys

from src.main import main

if __name__ == "__main__":
    sys.exit(main())
