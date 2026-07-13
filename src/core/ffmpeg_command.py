"""Dựng danh sách tham số dòng lệnh FFmpeg cho phiên phát.

Ý tưởng cốt lõi (kịch bản "cùng nội dung → nhiều kênh"):
  đọc playlist (concat) -> chuẩn hóa kích thước/fps -> ENCODE MỘT LẦN
  -> muxer `tee` fan-out ra N địa chỉ RTMP (mỗi kênh một stream key).

Mỗi output tee gắn `onfail=ignore` để một kênh rớt không làm sập cả cụm.
Tham số trả về là list (không qua shell) để đưa thẳng vào QProcess/subprocess.
"""
from __future__ import annotations

from .ffmpeg_locator import ffmpeg_path
from .models import YOUTUBE_RTMP_BASE, Channel, StreamConfig
from .presets import get_preset


def _video_filter(width: int, height: int, fps: int) -> str:
    """Scale giữ tỉ lệ + pad về đúng khung + ép fps + pixel format chuẩn."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p"
    )


def _video_encoder_args(encoder_key: str, bitrate_kbps: int, maxrate_kbps: int,
                        bufsize_kbps: int, gop: int) -> list[str]:
    """Tham số rate-control theo từng loại encoder, dạng CBR cho live."""
    b = f"{bitrate_kbps}k"
    maxrate = f"{maxrate_kbps}k"
    bufsize = f"{bufsize_kbps}k"

    common_tail = ["-b:v", b, "-maxrate", maxrate, "-bufsize", bufsize, "-g", str(gop)]

    if encoder_key == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "cbr", "-rc-lookahead", "8", *common_tail]
    if encoder_key == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", *common_tail]
    if encoder_key == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cbr", *common_tail]
    # CPU fallback
    return ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", *common_tail]


def _tee_target(channels: list[Channel], base: str = YOUTUBE_RTMP_BASE) -> str:
    """Chuỗi target cho muxer tee: nhiều output FLV, mỗi cái onfail=ignore."""
    parts = []
    for ch in channels:
        # Escape ký tự đặc biệt của cú pháp tee: \ | [ ]
        url = ch.rtmp_url(base)
        url = url.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
        parts.append(f"[f=flv:onfail=ignore]{url}")
    return "|".join(parts)


def build_command(cfg: StreamConfig, concat_file: str) -> list[str]:
    """Dựng đầy đủ tham số FFmpeg. Ném ValueError nếu cấu hình không hợp lệ."""
    channels = cfg.active_channels()
    if not channels:
        raise ValueError("Chưa có kênh hợp lệ nào (thiếu stream key).")
    if not cfg.playlist:
        raise ValueError("Playlist rỗng.")

    preset = get_preset(cfg.preset_key)
    bitrate = cfg.bitrate_override_kbps or preset.video_bitrate_kbps
    maxrate = bitrate
    bufsize = bitrate * 2

    args: list[str] = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "warning",
        "-progress", "pipe:1",   # tiến trình ra stdout dạng key=value để parse
        "-nostats",
        "-re",                   # đọc theo tốc độ thật (giả lập realtime)
    ]

    if cfg.loop:
        args += ["-stream_loop", "-1"]   # lặp toàn bộ playlist vô hạn

    # Đầu vào: concat demuxer
    args += [
        "-fflags", "+genpts",            # sinh lại PTS, tránh lỗi timestamp khi nối clip
        "-f", "concat", "-safe", "0", "-i", concat_file,
    ]

    # Video: lọc chuẩn hóa + encode một lần
    args += ["-vf", _video_filter(preset.width, preset.height, preset.fps)]
    args += _video_encoder_args(cfg.encoder_key, bitrate, maxrate, bufsize, preset.gop)

    # Audio: AAC chuẩn YouTube
    args += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]

    # Fan-out tee
    args += ["-f", "tee", "-map", "0:v:0", "-map", "0:a:0?", _tee_target(channels)]

    return args


def command_preview(cfg: StreamConfig, concat_file: str = "playlist.txt") -> str:
    """Chuỗi lệnh dễ đọc để hiển thị/nhật ký (che bớt stream key)."""
    try:
        args = build_command(cfg, concat_file)
    except ValueError as exc:
        return f"(không dựng được lệnh: {exc})"
    shown = []
    for a in args:
        if "rtmp://" in a:
            # che stream key trong preview
            import re
            a = re.sub(r"(live2/)[^|\]\s]+", r"\1********", a)
        shown.append(f'"{a}"' if " " in a else a)
    return " ".join(shown)
