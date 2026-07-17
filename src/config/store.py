"""Lưu/đọc cấu hình ra file JSON trong thư mục dữ liệu người dùng.

Vị trí: %APPDATA%/LiveYoutube/config.json (Windows) hoặc ~/.config/LiveYoutube.
Lưu cả stream key — đây là dữ liệu cục bộ trên máy người dùng.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from ..core.models import Channel, StreamConfig

APP_DIR_NAME = "LiveYoutube"


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / APP_DIR_NAME
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


def save(cfg: StreamConfig) -> None:
    data = {
        "playlist": list(cfg.playlist),
        "channels": [asdict(c) for c in cfg.channels],
        "preset_key": cfg.preset_key,
        "encoder_key": cfg.encoder_key,
        "loop": cfg.loop,
        "bitrate_override_kbps": cfg.bitrate_override_kbps,
        "auto_restart": cfg.auto_restart,
    }
    tmp = config_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config_path())


def load() -> StreamConfig:
    path = config_path()
    if not path.exists():
        return StreamConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return StreamConfig()

    channels = []
    for c in data.get("channels", []):
        ch = Channel(
            name=c.get("name", ""),
            stream_key=c.get("stream_key", ""),
            enabled=c.get("enabled", True),
        )
        if c.get("id"):
            ch.id = c["id"]
        channels.append(ch)
    return StreamConfig(
        playlist=list(data.get("playlist", [])),
        channels=channels,
        preset_key=data.get("preset_key", "2160p30"),
        encoder_key=data.get("encoder_key", "h264_nvenc"),
        loop=data.get("loop", True),
        bitrate_override_kbps=data.get("bitrate_override_kbps"),
        auto_restart=data.get("auto_restart", True),
    )
