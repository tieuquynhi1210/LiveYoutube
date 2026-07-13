"""Xác định đường dẫn tới ffmpeg/ffprobe.

Thứ tự tìm:
  1. FFmpeg đóng gói kèm trong resources/ffmpeg/ (khi build .exe).
  2. Biến môi trường LIVEYT_FFMPEG (nếu người dùng chỉ định).
  3. ffmpeg trên PATH hệ thống.
"""
from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path


def _project_root() -> Path:
    # src/core/ffmpeg_locator.py -> lên 3 cấp là gốc dự án
    return Path(__file__).resolve().parents[2]


def _bundled(name: str) -> Path | None:
    # Khi chạy dưới dạng .exe do PyInstaller đóng gói
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", _project_root()))
    else:
        base = _project_root()
    candidate = base / "resources" / "ffmpeg" / f"{name}.exe"
    return candidate if candidate.exists() else None


@lru_cache(maxsize=None)
def _locate(name: str) -> str:
    bundled = _bundled(name)
    if bundled is not None:
        return str(bundled)

    env = os.environ.get("LIVEYT_FFMPEG")
    if env:
        p = Path(env)
        # env có thể trỏ tới thư mục hoặc trực tiếp file ffmpeg
        if p.is_dir():
            cand = p / (f"{name}.exe" if os.name == "nt" else name)
            if cand.exists():
                return str(cand)
        elif p.exists() and name == "ffmpeg":
            return str(p)

    found = shutil.which(name)
    if found:
        return found

    # Fallback: tên trần, để lỗi hiện rõ khi chạy nếu thiếu
    return name


def ffmpeg_path() -> str:
    return _locate("ffmpeg")


def ffprobe_path() -> str:
    return _locate("ffprobe")


def ffmpeg_available() -> bool:
    return _bundled("ffmpeg") is not None or shutil.which("ffmpeg") is not None \
        or bool(os.environ.get("LIVEYT_FFMPEG"))
