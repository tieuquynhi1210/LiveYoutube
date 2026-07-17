"""Các kiểu dữ liệu cấu hình dùng chung."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# Endpoint ingest RTMP mặc định của YouTube Live.
YOUTUBE_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"
# Endpoint dự phòng (backup) của YouTube.
YOUTUBE_RTMP_BACKUP = "rtmp://b.rtmp.youtube.com/live2?backup=1"


@dataclass
class Channel:
    """Một kênh YouTube đích = tên gợi nhớ + stream key."""
    name: str
    stream_key: str
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def rtmp_url(self, base: str = YOUTUBE_RTMP_BASE) -> str:
        key = self.stream_key.strip()
        return f"{base}/{key}"

    @property
    def is_valid(self) -> bool:
        return bool(self.stream_key.strip())


@dataclass
class StreamConfig:
    """Toàn bộ cấu hình cho một phiên phát."""
    playlist: list[str] = field(default_factory=list)      # đường dẫn video
    channels: list[Channel] = field(default_factory=list)   # kênh đích
    preset_key: str = "2160p30"
    encoder_key: str = "h264_nvenc"
    loop: bool = True                                       # lặp playlist 24/7
    bitrate_override_kbps: int | None = None                # None = dùng theo preset
    auto_restart: bool = True                               # tự chạy lại khi FFmpeg thoát bất thường

    def active_channels(self) -> list[Channel]:
        return [c for c in self.channels if c.enabled and c.is_valid]
