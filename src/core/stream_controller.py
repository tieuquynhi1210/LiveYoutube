"""Điều phối phát đa kênh: 1 encoder + nhiều relay độc lập.

Kiến trúc:
  - ENCODER: đọc playlist, encode MỘT LẦN, xuất HLS nội bộ (thư mục tạm).
  - RELAY (mỗi kênh 1 tiến trình): đọc HLS đó, COPY (không encode lại) rồi đẩy
    RTMP lên kênh YouTube tương ứng.

Nhờ tách tiến trình:
  - Thêm / xoá / tạm dừng / phát tiếp TỪNG kênh khi đang live mà không đụng
    các kênh khác.
  - Một kênh nghẽn/rớt chỉ ảnh hưởng chính nó; mỗi relay có watchdog + tự nối lại.
  - Encoder cũng có watchdog riêng (treo -> kill + khởi động lại).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from enum import Enum

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import playlist_manager
from .ffmpeg_command import build_encoder_command, build_relay_command
from .ffmpeg_parser import ProgressParser, ProgressStats
from .models import Channel, StreamConfig

MAX_RESTARTS = 1000
ENCODER_RESTART_DELAY_MS = 3000
RELAY_RESTART_DELAY_MS = 3000
STALL_TIMEOUT_SEC = 20.0
RELAY_STALL_TIMEOUT_SEC = 25.0
WATCHDOG_INTERVAL_MS = 5000
HLS_WAIT_RETRY_MS = 1000


class StreamState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class RelayState(str, Enum):
    STARTING = "starting"        # đang khởi động / chờ nguồn
    LIVE = "live"                # đang phát lên kênh
    PAUSED = "paused"            # người dùng tạm dừng
    RECONNECTING = "reconnecting"  # rớt, đang tự nối lại
    ERROR = "error"
    STOPPED = "stopped"


# Nhãn tiếng Việt cho UI.
RELAY_STATE_LABEL = {
    RelayState.STARTING: "Đang khởi động…",
    RelayState.LIVE: "Đang phát",
    RelayState.PAUSED: "Tạm dừng",
    RelayState.RECONNECTING: "Đang nối lại…",
    RelayState.ERROR: "Lỗi",
    RelayState.STOPPED: "Đã dừng",
}


class _Relay(QObject):
    """Một relay = một tiến trình FFmpeg đẩy HLS nội bộ lên 1 kênh YouTube."""

    changed = Signal(str, object, str)   # channel_id, RelayState, message
    log = Signal(str)

    def __init__(self, channel: Channel, m3u8_path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self._m3u8 = m3u8_path
        self._proc: QProcess | None = None
        self._parser = ProgressParser()
        self._stdout_buf = ""
        self._state = RelayState.STOPPED
        self._intent = "stop"        # 'run' | 'pause' | 'stop'
        self._restart_count = 0
        self._last_progress_ts = 0.0
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_stall)

    @property
    def state(self) -> RelayState:
        return self._state

    @property
    def is_running_intent(self) -> bool:
        return self._intent == "run"

    # ------------------------------------------------------------ điều khiển
    def start(self) -> None:
        self._intent = "run"
        # Chờ encoder tạo xong m3u8 rồi mới chạy (tránh spawn tiến trình lỗi).
        if not os.path.exists(self._m3u8):
            self._set_state(RelayState.STARTING, "chờ nguồn từ encoder…")
            QTimer.singleShot(HLS_WAIT_RETRY_MS, self._start_if_still_wanted)
            return
        self._spawn()

    def _start_if_still_wanted(self) -> None:
        if self._intent == "run":
            self.start()

    def pause(self) -> None:
        self._intent = "pause"
        self._kill()
        if self._proc is None or self._proc.state() == QProcess.NotRunning:
            self._set_state(RelayState.PAUSED, "")

    def stop_remove(self) -> None:
        self._intent = "stop"
        self._kill()
        self._watchdog.stop()

    # ---------------------------------------------------------------- nội bộ
    def _spawn(self) -> None:
        args = build_relay_command(self.channel, self._m3u8)
        program, *proc_args = args
        self._parser = ProgressParser()
        self._stdout_buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        self._proc = proc
        self._set_state(RelayState.STARTING, "")
        self._last_progress_ts = time.monotonic()
        self._watchdog.start()
        proc.start(program, proc_args)

    def _on_stdout(self) -> None:
        if self._proc is None:
            return
        self._stdout_buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "ignore")
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            stats = self._parser.feed_line(line)
            if stats is not None:
                self._last_progress_ts = time.monotonic()
                if self._state in (RelayState.STARTING, RelayState.RECONNECTING):
                    self._restart_count = 0
                    self._set_state(RelayState.LIVE, "")

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "ignore")
        for line in data.splitlines():
            line = line.strip()
            if line:
                self.log.emit(f"[{self.channel.name or 'kênh'}] {line}")

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._watchdog.stop()
        if self._intent == "pause":
            self._set_state(RelayState.PAUSED, "")
            return
        if self._intent == "stop":
            self._set_state(RelayState.STOPPED, "")
            return
        # intent == run mà thoát -> tự nối lại
        if self._restart_count < MAX_RESTARTS:
            self._restart_count += 1
            self._set_state(RelayState.RECONNECTING,
                            f"nối lại lần {self._restart_count} sau {RELAY_RESTART_DELAY_MS // 1000}s…")
            QTimer.singleShot(RELAY_RESTART_DELAY_MS, self._start_if_still_wanted)
        else:
            self._set_state(RelayState.ERROR, "quá số lần thử lại")

    def _check_stall(self) -> None:
        if self._state != RelayState.LIVE:
            return
        if self._last_progress_ts and (time.monotonic() - self._last_progress_ts) > RELAY_STALL_TIMEOUT_SEC:
            self._watchdog.stop()
            self.log.emit(f"[{self.channel.name or 'kênh'}] treo (nghẽn) → nối lại.")
            self._kill()  # -> _on_finished -> reconnect

    def _kill(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self._proc.kill()

    def _set_state(self, state: RelayState, message: str) -> None:
        self._state = state
        self.changed.emit(self.channel.id, state, message)


class StreamController(QObject):
    state_changed = Signal(StreamState)
    encoder_stats = Signal(ProgressStats)
    channel_changed = Signal(str, object, str)   # id, RelayState, message
    log_line = Signal(str)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg: StreamConfig | None = None
        self._state = StreamState.IDLE

        # Encoder
        self._enc: QProcess | None = None
        self._enc_parser = ProgressParser()
        self._enc_buf = ""
        self._enc_user_stopping = False
        self._enc_restart_count = 0
        self._enc_last_progress_ts = 0.0
        self._enc_watchdog = QTimer(self)
        self._enc_watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._enc_watchdog.timeout.connect(self._check_encoder_stall)

        # Relay theo channel id
        self._relays: dict[str, _Relay] = {}

        # HLS tạm
        self._hls_dir: str | None = None
        self._m3u8: str | None = None
        self._concat_file: str | None = None

        # Resume (chỉ dùng cho encoder 1 clip)
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._total_duration = 0.0

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (StreamState.STARTING, StreamState.RUNNING)

    def relay_state(self, channel_id: str) -> RelayState:
        r = self._relays.get(channel_id)
        return r.state if r else RelayState.STOPPED

    def start(self, cfg: StreamConfig) -> None:
        if self.is_active:
            self.log_line.emit("Đang phát rồi, bỏ qua lệnh Start.")
            return
        if not cfg.playlist:
            self.error.emit("Playlist rỗng.")
            return
        if not cfg.active_channels():
            self.error.emit("Chưa có kênh hợp lệ nào (thiếu stream key).")
            return

        self._cfg = cfg
        self._enc_user_stopping = False
        self._enc_restart_count = 0
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._total_duration = 0.0
        self._compute_total_duration_async(cfg.playlist)

        self._hls_dir = tempfile.mkdtemp(prefix="liveyt_hls_")
        self._m3u8 = os.path.join(self._hls_dir, "stream.m3u8")
        self.log_line.emit(f"Nguồn HLS nội bộ: {self._hls_dir}")

        self._set_state(StreamState.STARTING)
        self._launch_encoder()

        # Khởi động relay cho từng kênh đang bật.
        self._relays.clear()
        for ch in cfg.active_channels():
            self._add_relay(ch, autostart=True)

    def stop(self) -> None:
        if self._state == StreamState.IDLE:
            return
        self._enc_user_stopping = True
        self._set_state(StreamState.STOPPING)
        self.log_line.emit("Đang dừng tất cả kênh và encoder…")
        for r in list(self._relays.values()):
            r.stop_remove()
        self._relays.clear()
        self._enc_watchdog.stop()
        if self._enc is not None and self._enc.state() != QProcess.NotRunning:
            self._enc.terminate()
            QTimer.singleShot(4000, self._force_kill_encoder)
        QTimer.singleShot(1200, self._finalize_idle)

    # ---- điều khiển từng kênh (dùng được cả khi đang live) ----
    def add_channel(self, channel: Channel) -> None:
        if self._cfg is not None and channel.id not in {c.id for c in self._cfg.channels}:
            self._cfg.channels.append(channel)
        if self.is_active and channel.is_valid:
            self._add_relay(channel, autostart=True)
            self.log_line.emit(f"Thêm kênh khi đang live: {channel.name or channel.id}")

    def remove_channel(self, channel_id: str) -> None:
        r = self._relays.pop(channel_id, None)
        if r is not None:
            r.stop_remove()
        if self._cfg is not None:
            self._cfg.channels = [c for c in self._cfg.channels if c.id != channel_id]

    def pause_channel(self, channel_id: str) -> None:
        r = self._relays.get(channel_id)
        if r is not None:
            r.pause()
            self.log_line.emit(f"Tạm dừng kênh {r.channel.name or channel_id}")

    def resume_channel(self, channel_id: str) -> None:
        r = self._relays.get(channel_id)
        if r is None and self._cfg is not None and self.is_active:
            # kênh chưa có relay (mới bật lại) -> tạo mới
            ch = next((c for c in self._cfg.channels if c.id == channel_id), None)
            if ch is not None:
                self._add_relay(ch, autostart=True)
                return
        if r is not None:
            r.start()
            self.log_line.emit(f"Phát tiếp kênh {r.channel.name or channel_id}")

    # -------------------------------------------------------------- relay mgmt
    def _add_relay(self, channel: Channel, autostart: bool) -> None:
        if self._m3u8 is None:
            return
        r = _Relay(channel, self._m3u8, self)
        r.changed.connect(self.channel_changed)
        r.log.connect(self.log_line)
        self._relays[channel.id] = r
        if autostart:
            r.start()

    # ---------------------------------------------------------- encoder mgmt
    def _launch_encoder(self) -> None:
        assert self._cfg is not None and self._hls_dir is not None
        try:
            self._concat_file = playlist_manager.write_concat_file(self._cfg.playlist)
            args = build_encoder_command(self._cfg, self._concat_file, self._hls_dir,
                                         self._resume_offset)
        except ValueError as exc:
            self.error.emit(str(exc))
            self._set_state(StreamState.ERROR)
            return

        program, *proc_args = args
        self._enc_parser = ProgressParser()
        self._enc_buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_enc_stdout)
        proc.readyReadStandardError.connect(self._on_enc_stderr)
        proc.finished.connect(self._on_enc_finished)
        proc.errorOccurred.connect(self._on_enc_error)
        self._enc = proc
        self.log_line.emit(
            f"Khởi động encoder: preset {self._cfg.preset_key}, encoder {self._cfg.encoder_key}."
        )
        if self._resume_offset > 0.5:
            m, s = divmod(int(self._resume_offset), 60)
            self.log_line.emit(f"Encoder phát tiếp từ {m:02d}:{s:02d}.")
        proc.start(program, proc_args)

    def _on_enc_stdout(self) -> None:
        if self._enc is None:
            return
        self._enc_buf += bytes(self._enc.readAllStandardOutput()).decode("utf-8", "ignore")
        while "\n" in self._enc_buf:
            line, self._enc_buf = self._enc_buf.split("\n", 1)
            stats = self._enc_parser.feed_line(line)
            if stats is not None:
                self._enc_last_progress_ts = time.monotonic()
                self._last_out_time = stats.out_time_sec
                if self._state == StreamState.STARTING:
                    self._set_state(StreamState.RUNNING)
                    self._enc_restart_count = 0
                    self._enc_watchdog.start()
                self.encoder_stats.emit(stats)

    def _on_enc_stderr(self) -> None:
        if self._enc is None:
            return
        data = bytes(self._enc.readAllStandardError()).decode("utf-8", "ignore")
        for line in data.splitlines():
            line = line.strip()
            if line:
                self.log_line.emit(f"[encoder] {line}")

    def _on_enc_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.FailedToStart:
            self.error.emit("Không khởi động được FFmpeg (encoder). Kiểm tra FFmpeg.")
            self._set_state(StreamState.ERROR)

    def _on_enc_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self.log_line.emit(f"Encoder kết thúc (mã {code}).")
        if self._enc_user_stopping:
            return
        cfg = self._cfg
        clean_end = (status == QProcess.NormalExit and code == 0)
        if clean_end and cfg is not None and not cfg.loop:
            self.log_line.emit("Đã phát hết playlist.")
            self.stop()
            return
        if cfg is not None and self._enc_restart_count < MAX_RESTARTS:
            self._enc_restart_count += 1
            if clean_end and cfg.loop:
                self._resume_offset = 0.0
                self._last_out_time = 0.0
                self.log_line.emit("Hết playlist — lặp lại từ đầu…")
                QTimer.singleShot(300, self._relaunch_encoder)
            else:
                self._update_resume_offset(cfg)
                self.log_line.emit(
                    f"Encoder mất luồng — khởi động lại lần {self._enc_restart_count} "
                    f"sau {ENCODER_RESTART_DELAY_MS // 1000}s…"
                )
                QTimer.singleShot(ENCODER_RESTART_DELAY_MS, self._relaunch_encoder)
        else:
            self.error.emit("Encoder lỗi quá nhiều lần. Dừng.")
            self.stop()

    def _relaunch_encoder(self) -> None:
        if self._enc_user_stopping:
            return
        self._set_state(StreamState.STARTING)
        self._launch_encoder()

    def _check_encoder_stall(self) -> None:
        if self._state != StreamState.RUNNING:
            return
        if self._enc_last_progress_ts and \
                (time.monotonic() - self._enc_last_progress_ts) > STALL_TIMEOUT_SEC:
            self._enc_watchdog.stop()
            self.log_line.emit("⚠ Encoder treo — kill và khởi động lại.")
            if self._enc is not None and self._enc.state() != QProcess.NotRunning:
                self._enc.kill()

    def _force_kill_encoder(self) -> None:
        if self._enc is not None and self._enc.state() != QProcess.NotRunning:
            self._enc.kill()

    # -------------------------------------------------------------- resume/dur
    def _compute_total_duration_async(self, playlist: list[str]) -> None:
        def worker(paths: list[str]) -> None:
            total = 0.0
            for p in paths:
                info = playlist_manager.probe(p)
                if info.duration_sec:
                    total += info.duration_sec
            self._total_duration = total
        threading.Thread(target=worker, args=(list(playlist),), daemon=True).start()

    def _update_resume_offset(self, cfg: StreamConfig) -> None:
        # Chỉ có ý nghĩa với encoder 1 clip (demuxer + -ss); nhiều clip bỏ qua.
        if len(cfg.playlist) >= 2:
            self._resume_offset = 0.0
            self._last_out_time = 0.0
            return
        absolute = self._resume_offset + self._last_out_time
        total = self._total_duration
        if total > 1.0 and cfg.loop:
            offset = absolute % total
        elif total > 1.0:
            offset = absolute if absolute < total else 0.0
        else:
            offset = 0.0
        self._resume_offset = max(0.0, offset)
        self._last_out_time = 0.0

    # -------------------------------------------------------------- finalize
    def _finalize_idle(self) -> None:
        self._force_kill_encoder()
        self._cleanup_files()
        self._set_state(StreamState.IDLE)
        self.log_line.emit("Đã dừng.")

    def _cleanup_files(self) -> None:
        if self._concat_file and os.path.exists(self._concat_file):
            try:
                os.remove(self._concat_file)
            except OSError:
                pass
        self._concat_file = None
        if self._hls_dir and os.path.isdir(self._hls_dir):
            shutil.rmtree(self._hls_dir, ignore_errors=True)
        self._hls_dir = None
        self._m3u8 = None

    def _set_state(self, state: StreamState) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
