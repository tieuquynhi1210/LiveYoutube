"""Preset chất lượng cho YouTube Live.

Bitrate tham khảo theo khuyến nghị của YouTube cho từng độ phân giải/khung hình.
Mỗi preset là bitrate video mục tiêu (kbps). Audio cố định 128 kbps AAC 48kHz.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    key: str          # định danh nội bộ
    label: str        # nhãn hiển thị trên UI
    width: int
    height: int
    fps: int
    video_bitrate_kbps: int  # bitrate video mục tiêu

    @property
    def maxrate_kbps(self) -> int:
        # CBR: maxrate = bitrate mục tiêu
        return self.video_bitrate_kbps

    @property
    def bufsize_kbps(self) -> int:
        # buffer = 2x bitrate cho luồng ổn định
        return self.video_bitrate_kbps * 2

    @property
    def gop(self) -> int:
        # YouTube khuyến nghị keyframe mỗi 2 giây
        return self.fps * 2


# Danh sách preset, sắp theo độ nặng giảm dần.
PRESETS: list[Preset] = [
    Preset("2160p60", "4K • 2160p60", 3840, 2160, 60, 40000),
    Preset("2160p30", "4K • 2160p30", 3840, 2160, 30, 25000),
    Preset("1440p60", "2K • 1440p60", 2560, 1440, 60, 16000),
    Preset("1440p30", "2K • 1440p30", 2560, 1440, 30, 12000),
    Preset("1080p60", "Full HD • 1080p60", 1920, 1080, 60, 9000),
    Preset("1080p30", "Full HD • 1080p30", 1920, 1080, 30, 6000),
    Preset("720p30", "HD • 720p30", 1280, 720, 30, 4000),
]

PRESET_BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}

DEFAULT_PRESET_KEY = "2160p30"


def get_preset(key: str) -> Preset:
    return PRESET_BY_KEY.get(key, PRESET_BY_KEY[DEFAULT_PRESET_KEY])
