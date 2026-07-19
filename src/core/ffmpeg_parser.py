"""Phân tích luồng tiến trình `-progress pipe:1` của FFmpeg.

FFmpeg in ra từng block key=value, kết thúc bằng dòng `progress=continue`
hoặc `progress=end`. Ví dụ một block:

    frame=120
    fps=30.00
    bitrate=25000.0kbits/s
    total_size=1234567
    out_time_us=4000000
    out_time=00:00:04.000000
    dup_frames=0
    drop_frames=0
    speed=1.00x
    progress=continue
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProgressStats:
    frame: int = 0
    fps: float = 0.0
    bitrate_kbps: float = 0.0
    out_time_sec: float = 0.0
    dropped_frames: int = 0
    dup_frames: int = 0
    speed: float = 0.0
    ended: bool = False

    @property
    def uptime_hms(self) -> str:
        s = int(self.out_time_sec)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    @property
    def drop_rate(self) -> float:
        """Tỉ lệ frame rớt (%) trên tổng số frame đã xử lý."""
        total = self.frame + self.dropped_frames
        return (self.dropped_frames / total * 100.0) if total > 0 else 0.0


class ProgressParser:
    """Nạp từng dòng, phát ProgressStats mỗi khi một block hoàn tất."""

    def __init__(self) -> None:
        self._acc: dict[str, str] = {}

    def feed_line(self, line: str) -> ProgressStats | None:
        line = line.strip()
        if not line or "=" not in line:
            return None
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        self._acc[key] = value

        if key == "progress":
            stats = self._build(ended=(value == "end"))
            self._acc.clear()
            return stats
        return None

    def _build(self, ended: bool) -> ProgressStats:
        a = self._acc
        return ProgressStats(
            frame=_to_int(a.get("frame")),
            fps=_to_float(a.get("fps")),
            bitrate_kbps=_parse_bitrate(a.get("bitrate")),
            out_time_sec=_parse_time_us(a.get("out_time_us"), a.get("out_time")),
            dropped_frames=_to_int(a.get("drop_frames")),
            dup_frames=_to_int(a.get("dup_frames")),
            speed=_parse_speed(a.get("speed")),
            ended=ended,
        )


def _to_int(v: str | None) -> int:
    try:
        return int(v) if v not in (None, "N/A") else 0
    except (TypeError, ValueError):
        return 0


def _to_float(v: str | None) -> float:
    try:
        return float(v) if v not in (None, "N/A") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_bitrate(v: str | None) -> float:
    # ví dụ "25000.0kbits/s"
    if not v or v == "N/A":
        return 0.0
    v = v.replace("kbits/s", "").strip()
    return _to_float(v)


def _parse_speed(v: str | None) -> float:
    # ví dụ "1.00x"
    if not v or v == "N/A":
        return 0.0
    return _to_float(v.replace("x", "").strip())


def _parse_time_us(us: str | None, hms: str | None) -> float:
    if us and us != "N/A":
        return _to_int(us) / 1_000_000.0
    if hms and hms != "N/A":
        try:
            h, m, s = hms.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except ValueError:
            return 0.0
    return 0.0
