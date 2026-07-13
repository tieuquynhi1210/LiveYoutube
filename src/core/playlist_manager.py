"""Quản lý playlist: sinh file concat cho FFmpeg và đọc thông tin media.

FFmpeg concat demuxer đọc một file text liệt kê các video theo thứ tự:

    ffconcat version 1.0
    file 'C:\\path\\video1.mp4'
    file 'C:\\path\\video2.mp4'

Vì luồng ra được re-encode qua một encoder duy nhất (+ bộ lọc chuẩn hóa
kích thước/fps), các video nguồn có thông số khác nhau vẫn nối được liền mạch.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_locator import ffprobe_path

_CREATE_NO_WINDOW = 0x08000000

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".ts", ".webm", ".wmv", ".mpg", ".mpeg",
}


@dataclass
class MediaInfo:
    path: str
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = False
    error: str | None = None

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "?"

    @property
    def duration_hms(self) -> str:
        if self.duration_sec is None:
            return "?"
        s = int(self.duration_sec)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def probe(path: str) -> MediaInfo:
    """Đọc thông tin video bằng ffprobe. Không ném lỗi — gói vào MediaInfo.error."""
    info = MediaInfo(path=path)
    if not Path(path).exists():
        info.error = "Không tìm thấy file"
        return info
    args = [
        ffprobe_path(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    try:
        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW, timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        info.error = f"ffprobe lỗi: {exc}"
        return info
    if proc.returncode != 0:
        info.error = proc.stderr.decode("utf-8", "ignore").strip() or "ffprobe thất bại"
        return info

    try:
        data = json.loads(proc.stdout.decode("utf-8", "ignore"))
    except json.JSONDecodeError:
        info.error = "Không đọc được dữ liệu ffprobe"
        return info

    fmt = data.get("format", {})
    if "duration" in fmt:
        try:
            info.duration_sec = float(fmt["duration"])
        except (TypeError, ValueError):
            pass

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video" and info.width is None:
            info.width = stream.get("width")
            info.height = stream.get("height")
            info.fps = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
        elif codec_type == "audio":
            info.has_audio = True
    return info


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    try:
        if "/" in rate:
            num, den = rate.split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


def _escape_concat(path: str) -> str:
    # Trong file concat, ký tự ' và \ cần escape; dùng đường dẫn tuyệt đối.
    abs_path = str(Path(path).resolve())
    escaped = abs_path.replace("\\", "\\\\").replace("'", "'\\''")
    return escaped


def write_concat_file(paths: list[str], directory: str | None = None) -> str:
    """Ghi file concat cho danh sách video; trả về đường dẫn file tạm.

    directory=None -> dùng thư mục tạm hệ thống.
    """
    if not paths:
        raise ValueError("Playlist rỗng")
    fd, name = tempfile.mkstemp(prefix="liveyt_playlist_", suffix=".txt", dir=directory)
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write("ffconcat version 1.0\n")
        for p in paths:
            fh.write(f"file '{_escape_concat(p)}'\n")
    return name
