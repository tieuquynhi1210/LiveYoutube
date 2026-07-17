"""Dựng danh sách tham số dòng lệnh FFmpeg cho phiên phát.

Ý tưởng cốt lõi (kịch bản "cùng nội dung → nhiều kênh"):
  đọc playlist -> chuẩn hóa kích thước/fps -> ENCODE MỘT LẦN
  -> muxer `tee` fan-out ra N địa chỉ RTMP (mỗi kênh một stream key).

Có 2 cách ghép clip:
  - 1 clip: concat demuxer + -stream_loop (lặp mượt), hỗ trợ tua -ss.
  - nhiều clip: concat FILTER — giải mã TỪNG clip bằng đúng codec của nó rồi
    mới ghép, nên KHÔNG bị màn hình đen ở điểm chuyển khi các clip khác codec
    (vd H.264 sang HEVC) hoặc khác thông số.

Mỗi output tee gắn `onfail=ignore` + `use_fifo` để một kênh rớt không kéo cả cụm.
Tham số trả về là list (không qua shell) để đưa thẳng vào QProcess/subprocess.
"""
from __future__ import annotations

from .ffmpeg_locator import ffmpeg_path
from .models import YOUTUBE_RTMP_BASE, Channel, StreamConfig
from .presets import get_preset

# Tùy chọn cho fifo muxer bọc quanh mỗi output tee:
#  - queue_size: đệm ~600 gói mỗi kênh (đủ hấp thụ giật mạng vài giây)
#  - drop_pkts_on_overflow: nghẽn quá lâu thì bỏ gói kênh đó (không chặn kênh khác)
#  - attempt_recovery + recover_any_error: rớt kết nối thì tự nối lại
#  - restart_with_keyframe: nối lại bắt đầu từ keyframe để hình không vỡ
FIFO_OPTIONS = (
    "queue_size=600:drop_pkts_on_overflow=1:attempt_recovery=1:"
    "recover_any_error=1:restart_with_keyframe=1"
)


def _video_filter(width: int, height: int, fps: int) -> str:
    """Scale giữ tỉ lệ + pad về đúng khung + ép fps + pixel format + SAR chuẩn."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p,setsar=1"
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


def _tee_output_args(cfg: StreamConfig, channels: list[Channel],
                     maps: list[str]) -> list[str]:
    """Phần đuôi chung: encoder video + audio AAC + fan-out tee (use_fifo)."""
    preset = get_preset(cfg.preset_key)
    bitrate = cfg.bitrate_override_kbps or preset.video_bitrate_kbps

    args: list[str] = list(maps)
    args += _video_encoder_args(cfg.encoder_key, bitrate, bitrate, bitrate * 2, preset.gop)
    args += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    args += [
        "-f", "tee",
        "-use_fifo", "1",
        "-fifo_options", FIFO_OPTIONS,
        _tee_target(channels),
    ]
    return args


def build_command(cfg: StreamConfig, concat_file: str,
                  resume_offset: float = 0.0) -> list[str]:
    """Dựng đầy đủ tham số FFmpeg. Ném ValueError nếu cấu hình không hợp lệ."""
    channels = cfg.active_channels()
    if not channels:
        raise ValueError("Chưa có kênh hợp lệ nào (thiếu stream key).")
    if not cfg.playlist:
        raise ValueError("Playlist rỗng.")

    if len(cfg.playlist) >= 2:
        return _build_filter_command(cfg, channels)
    return _build_demuxer_command(cfg, concat_file, channels, resume_offset)


def _build_demuxer_command(cfg: StreamConfig, concat_file: str,
                           channels: list[Channel], resume_offset: float) -> list[str]:
    """Nhánh 1 clip: concat demuxer, lặp bằng -stream_loop, tua bằng -ss."""
    preset = get_preset(cfg.preset_key)
    args: list[str] = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats", "-re",
    ]
    if cfg.loop:
        args += ["-stream_loop", "-1"]
    if resume_offset > 0.5:
        args += ["-ss", f"{resume_offset:.3f}"]
    args += ["-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", concat_file]
    args += ["-vf", _video_filter(preset.width, preset.height, preset.fps)]
    args += _tee_output_args(cfg, channels, ["-map", "0:v:0", "-map", "0:a:0?"])
    return args


def _build_filter_command(cfg: StreamConfig, channels: list[Channel]) -> list[str]:
    """Nhánh nhiều clip: concat filter — mỗi clip giải mã riêng rồi ghép.

    Clip thiếu tiếng được cấp nguồn im lặng (anullsrc) đúng thời lượng, để
    concat filter (yêu cầu mọi đoạn có cả video lẫn audio) không lỗi.
    """
    from . import playlist_manager  # tránh import vòng ở cấp module

    preset = get_preset(cfg.preset_key)
    paths = cfg.playlist
    infos = [playlist_manager.probe(p) for p in paths]
    vf = _video_filter(preset.width, preset.height, preset.fps)

    args: list[str] = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats",
    ]
    # Đầu vào các file thật (index 0..N-1), mỗi cái đọc theo tốc độ thật.
    for p in paths:
        args += ["-re", "-i", p]

    # Nguồn im lặng cho clip thiếu tiếng (index N trở đi).
    silent_input_of: dict[int, int] = {}
    next_index = len(paths)
    for i, info in enumerate(infos):
        if not info.has_audio:
            dur = info.duration_sec or 36000.0
            args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            silent_input_of[i] = next_index
            next_index += 1

    # filter_complex: chuẩn hóa từng clip rồi concat.
    parts: list[str] = []
    concat_in = ""
    for i, info in enumerate(infos):
        parts.append(f"[{i}:v]{vf}[v{i}]")
        a_src = i if info.has_audio else silent_input_of[i]
        parts.append(
            f"[{a_src}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
        )
        concat_in += f"[v{i}][a{i}]"
    parts.append(f"{concat_in}concat=n={len(paths)}:v=1:a=1[vout][aout]")

    args += ["-filter_complex", ";".join(parts)]
    args += _tee_output_args(cfg, channels, ["-map", "[vout]", "-map", "[aout]"])
    return args


def command_preview(cfg: StreamConfig, concat_file: str = "playlist.txt") -> str:
    """Chuỗi lệnh dễ đọc để hiển thị/nhật ký (che bớt stream key)."""
    import re
    try:
        args = build_command(cfg, concat_file)
    except ValueError as exc:
        return f"(không dựng được lệnh: {exc})"
    shown = []
    for a in args:
        if "rtmp://" in a:
            a = re.sub(r"(live2/)[^|\]\s]+", r"\1********", a)
        shown.append(f'"{a}"' if " " in a else a)
    return " ".join(shown)
