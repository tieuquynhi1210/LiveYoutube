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


def _encode_args(cfg: StreamConfig) -> list[str]:
    """Tham số encode video + audio (không kèm output)."""
    preset = get_preset(cfg.preset_key)
    bitrate = cfg.bitrate_override_kbps or preset.video_bitrate_kbps
    args = _video_encoder_args(cfg.encoder_key, bitrate, bitrate, bitrate * 2, preset.gop)
    args += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    return args


def _hls_output_args(hls_dir: str) -> list[str]:
    """Xuất HLS rolling nội bộ (encoder ghi, các relay đọc chung)."""
    seg = f"{hls_dir}/seg_%05d.ts"
    m3u8 = f"{hls_dir}/stream.m3u8"
    return [
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments+omit_endlist",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", seg,
        m3u8,
    ]


def build_encoder_command(cfg: StreamConfig, concat_file: str, hls_dir: str,
                          resume_offset: float = 0.0) -> list[str]:
    """Encoder: đọc playlist -> encode MỘT LẦN -> xuất HLS nội bộ trong hls_dir.

    - 1 clip: concat demuxer + -stream_loop (lặp mượt) + -ss (tua resume).
    - nhiều clip: concat FILTER (giải mã từng clip đúng codec, chống đen ở
      điểm chuyển). Lặp toàn playlist do lớp tự-khởi-động-lại đảm nhiệm.
    """
    if not cfg.playlist:
        raise ValueError("Playlist rỗng.")

    if len(cfg.playlist) >= 2:
        args = _encoder_input_filter(cfg)
        maps = ["-map", "[vout]", "-map", "[aout]"]
    else:
        args = _encoder_input_demuxer(cfg, concat_file, resume_offset)
        maps = ["-map", "0:v:0", "-map", "0:a:0?"]

    args += maps
    args += _encode_args(cfg)
    args += _hls_output_args(hls_dir)
    return args


def build_relay_command(channel: Channel, m3u8_path: str) -> list[str]:
    """Relay: đọc HLS nội bộ, COPY (không encode lại) -> đẩy RTMP lên 1 kênh.

    Không dùng -re: HLS live tự pace theo tốc độ segment ra.
    """
    return [
        ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats",
        "-i", m3u8_path,
        "-c", "copy",
        "-f", "flv", channel.rtmp_url(),
    ]


def _encoder_input_demuxer(cfg: StreamConfig, concat_file: str,
                           resume_offset: float) -> list[str]:
    """Nhánh 1 clip: concat demuxer + chuẩn hóa (input tới hết -vf)."""
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
    return args


def _encoder_input_filter(cfg: StreamConfig) -> list[str]:
    """Nhánh nhiều clip: concat filter — mỗi clip giải mã riêng rồi ghép.

    Clip thiếu tiếng được cấp nguồn im lặng (anullsrc) đúng thời lượng.
    """
    from . import playlist_manager  # tránh import vòng ở cấp module

    preset = get_preset(cfg.preset_key)
    paths = cfg.playlist
    infos = [playlist_manager.probe(p) for p in paths]
    vf = _video_filter(preset.width, preset.height, preset.fps)

    # KHÔNG dùng -re trên từng input (gây kẹt nhịp ở điểm chuyển clip). Thay
    # vào đó pace output bằng bộ lọc realtime/arealtime — cách chuẩn cho filtergraph.
    args: list[str] = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats",
    ]
    for p in paths:
        args += ["-i", p]

    silent_input_of: dict[int, int] = {}
    next_index = len(paths)
    for i, info in enumerate(infos):
        if not info.has_audio:
            dur = info.duration_sec or 36000.0
            args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            silent_input_of[i] = next_index
            next_index += 1

    parts: list[str] = []
    concat_in = ""
    for i, info in enumerate(infos):
        # :v:0 lấy luồng video thật (bỏ qua ảnh bìa nhúng nếu có).
        parts.append(f"[{i}:v:0]{vf}[v{i}]")
        a_src = i if info.has_audio else silent_input_of[i]
        parts.append(
            f"[{a_src}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
        )
        concat_in += f"[v{i}][a{i}]"
    parts.append(f"{concat_in}concat=n={len(paths)}:v=1:a=1[vc][ac]")
    parts.append("[vc]realtime[vout]")   # pace video về đúng tốc độ thật
    parts.append("[ac]arealtime[aout]")  # pace audio tương ứng

    args += ["-filter_complex", ";".join(parts)]
    return args
