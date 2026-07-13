"""Dò encoder phần cứng khả dụng trên máy.

Chiến lược 2 bước:
  1. Liệt kê encoder mà bản FFmpeg này biên dịch kèm (`ffmpeg -encoders`).
  2. Test-encode thực tế một clip nhỏ để xác nhận encoder CHẠY ĐƯỢC
     (có encoder trong danh sách không đồng nghĩa với GPU/driver hỗ trợ).

Ưu tiên: NVENC (NVIDIA) > QSV (Intel) > AMF (AMD) > libx264 (CPU).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .ffmpeg_locator import ffmpeg_path

# Cửa sổ tạo tiến trình ẩn trên Windows (không bật console đen).
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Encoder:
    key: str            # tên codec truyền cho ffmpeg -c:v
    label: str          # nhãn hiển thị
    kind: str           # 'nvenc' | 'qsv' | 'amf' | 'cpu'
    is_hardware: bool


# Thứ tự ưu tiên. Phần tử đầu được ưu tiên nhất.
_CANDIDATES: list[Encoder] = [
    Encoder("h264_nvenc", "NVIDIA NVENC (H.264)", "nvenc", True),
    Encoder("h264_qsv", "Intel QuickSync (H.264)", "qsv", True),
    Encoder("h264_amf", "AMD AMF (H.264)", "amf", True),
    Encoder("libx264", "CPU (x264, phần mềm)", "cpu", False),
]

# libx264 luôn coi là khả dụng (fallback), khỏi test.
_ALWAYS_AVAILABLE = {"libx264"}


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        timeout=timeout,
    )


def _compiled_encoders() -> set[str]:
    """Tập tên encoder mà FFmpeg biên dịch kèm."""
    try:
        proc = _run([ffmpeg_path(), "-hide_banner", "-encoders"])
    except (OSError, subprocess.TimeoutExpired):
        return set()
    out = proc.stdout.decode("utf-8", "ignore")
    found: set[str] = set()
    for enc in _CANDIDATES:
        if enc.key in out:
            found.add(enc.key)
    return found


def _test_encode(encoder_key: str) -> bool:
    """Encode thử 1 clip test nhỏ; True nếu FFmpeg trả về mã 0."""
    args = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=256x256:rate=30",
        "-c:v", encoder_key, "-f", "null", "-",
    ]
    try:
        proc = _run(args, timeout=30.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def detect_encoders(functional_test: bool = True) -> list[Encoder]:
    """Trả về danh sách encoder khả dụng theo thứ tự ưu tiên.

    functional_test=False: chỉ dựa vào danh sách biên dịch (nhanh, dùng cho UI
    khởi động); functional_test=True: encode thử để chắc chắn (chậm hơn).
    """
    compiled = _compiled_encoders()
    result: list[Encoder] = []
    for enc in _CANDIDATES:
        if enc.key in _ALWAYS_AVAILABLE:
            result.append(enc)
            continue
        if enc.key not in compiled:
            continue
        if functional_test and not _test_encode(enc.key):
            continue
        result.append(enc)
    return result


def best_encoder(functional_test: bool = True) -> Encoder:
    """Encoder tốt nhất khả dụng (ưu tiên phần cứng)."""
    encoders = detect_encoders(functional_test=functional_test)
    return encoders[0] if encoders else _CANDIDATES[-1]  # fallback libx264


ENCODER_BY_KEY: dict[str, Encoder] = {e.key: e for e in _CANDIDATES}
