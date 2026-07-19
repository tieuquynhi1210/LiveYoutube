"""Điều phối phát đa kênh: mỗi kênh là một tiến trình encode ĐỘC LẬP.

Mỗi kênh tự đọc playlist -> encode -> đẩy THẲNG RTMP lên kênh YouTube của nó
(giống cách đã lên sóng được ở bản đầu, nên YouTube nhận chắc chắn, trễ thấp).

Nhờ mỗi kênh một tiến trình riêng:
  - Thêm / xoá / tạm dừng / phát tiếp TỪNG kênh khi đang live, không đụng kênh khác.
  - Một kênh nghẽn/rớt chỉ ảnh hưởng chính nó; mỗi kênh có watchdog + tự nối lại,
    tự lặp playlist, và (với playlist 1 clip) tự phát tiếp từ chỗ dừng.

Đánh đổi: mỗi kênh encode riêng nên tốn GPU theo số kênh.
"""
from __future__ import annotations

import os
import threading
import time
from enum import Enum

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import playlist_manager
from .ffmpeg_command import build_channel_command
from .ffmpeg_parser import ProgressParser, ProgressStats
from .models import Channel, StreamConfig

MAX_RESTARTS = 1000
RESTART_DELAY_MS = 3000
STALL_TIMEOUT_SEC = 20.0
WATCHDOG_INTERVAL_MS = 5000


class StreamState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class RelayState(str, Enum):
    STARTING = "starting"
    LIVE = "live"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    STOPPED = "stopped"


RELAY_STATE_LABEL = {
    RelayState.STARTING: "Đang khởi động…",
    RelayState.LIVE: "Đang phát",
    RelayState.PAUSED: "Tạm dừng",
    RelayState.RECONNECTING: "Đang nối lại…",
    RelayState.ERROR: "Lỗi",
    RelayState.STOPPED: "Đã dừng",
}


class _ChannelStream(QObject):
    """Một kênh = một tiến trình FFmpeg encode & đẩy RTMP độc lập."""

    changed = Signal(str, object, str)   # channel_id, RelayState, message
    log = Signal(str)
    stats = Signal(object)               # ProgressStats

    def __init__(self, channel: Channel, cfg: StreamConfig, concat_file: str,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self._cfg = cfg
        self._concat_file = concat_file
        self.total_duration = 0.0

        self._proc: QProcess | None = None
        self._parser = ProgressParser()
        self._buf = ""
        self._state = RelayState.STOPPED
        self._intent = "stop"            # 'run' | 'pause' | 'stop'
        self._restart_count = 0
        self._last_progress_ts = 0.0
        self._resume_offset = 0.0
        self._last_out_time = 0.0

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_stall)

    @property
    def state(self) -> RelayState:
        return self._state

    # ------------------------------------------------------------ điều khiển
    def start_fresh(self) -> None:
        """Khởi động từ đầu playlist."""
        self._intent = "run"
        self._restart_count = 0
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._spawn()

    def resume(self) -> None:
        """Phát tiếp (sau khi tạm dừng) từ vị trí đã lưu."""
        self._intent = "run"
        self._restart_count = 0
        self._spawn()

    def pause(self) -> None:
        self._intent = "pause"
        self._update_resume_offset()
        self._kill()
        if self._proc is None or self._proc.state() == QProcess.NotRunning:
            self._set_state(RelayState.PAUSED, "")

    def stop_remove(self) -> None:
        self._intent = "stop"
        self._watchdog.stop()
        self._kill()

    # ---------------------------------------------------------------- nội bộ
    def _spawn(self) -> None:
        try:
            args = build_channel_command(self._cfg, self._concat_file, self.channel,
                                         self._resume_offset)
        except ValueError as exc:
            self._set_state(RelayState.ERROR, str(exc))
            return
        program, *proc_args = args
        self._parser = ProgressParser()
        self._buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        self._proc = proc
        msg = ""
        if self._resume_offset > 0.5:
            m, s = divmod(int(self._resume_offset), 60)
            msg = f"phát tiếp từ {m:02d}:{s:02d}"
        self._set_state(RelayState.STARTING, msg)
        self._last_progress_ts = time.monotonic()
        self._watchdog.start()
        proc.start(program, proc_args)

    def _on_stdout(self) -> None:
        if self._proc is None:
            return
        self._buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "ignore")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            st = self._parser.feed_line(line)
            if st is not None:
                self._last_progress_ts = time.monotonic()
                self._last_out_time = st.out_time_sec
                if self._state in (RelayState.STARTING, RelayState.RECONNECTING):
                    self._restart_count = 0
                    self._set_state(RelayState.LIVE, "")
                self.stats.emit(st)

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

        clean_end = (status == QProcess.NormalExit and code == 0)
        # Hết playlist mà không lặp -> kênh dừng hẳn.
        if clean_end and not self._cfg.loop:
            self._set_state(RelayState.STOPPED, "hết playlist")
            return
        if self._restart_count >= MAX_RESTARTS:
            self._set_state(RelayState.ERROR, "quá số lần thử lại")
            return
        self._restart_count += 1
        if clean_end and self._cfg.loop:
            self._resume_offset = 0.0
            self._last_out_time = 0.0
            self._set_state(RelayState.RECONNECTING, "hết playlist — lặp lại")
            QTimer.singleShot(300, self._spawn_if_run)
        else:
            self._update_resume_offset()
            self._set_state(RelayState.RECONNECTING,
                            f"nối lại lần {self._restart_count} sau {RESTART_DELAY_MS // 1000}s")
            QTimer.singleShot(RESTART_DELAY_MS, self._spawn_if_run)

    def _spawn_if_run(self) -> None:
        if self._intent == "run":
            self._spawn()

    def _check_stall(self) -> None:
        if self._state != RelayState.LIVE:
            return
        if self._last_progress_ts and \
                (time.monotonic() - self._last_progress_ts) > STALL_TIMEOUT_SEC:
            self._watchdog.stop()
            self.log.emit(f"[{self.channel.name or 'kênh'}] treo (nghẽn) → nối lại.")
            self._kill()  # -> _on_finished -> reconnect

    def _update_resume_offset(self) -> None:
        # Chỉ có ý nghĩa với playlist 1 clip (demuxer + -ss); nhiều clip -> từ đầu.
        if len(self._cfg.playlist) >= 2:
            self._resume_offset = 0.0
            self._last_out_time = 0.0
            return
        absolute = self._resume_offset + self._last_out_time
        total = self.total_duration
        if total > 1.0 and self._cfg.loop:
            offset = absolute % total
        elif total > 1.0:
            offset = absolute if absolute < total else 0.0
        else:
            offset = 0.0
        self._resume_offset = max(0.0, offset)
        self._last_out_time = 0.0

    def _kill(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self._proc.kill()

    def _set_state(self, state: RelayState, message: str) -> None:
        self._state = state
        self.changed.emit(self.channel.id, state, message)


class StreamController(QObject):
    state_changed = Signal(StreamState)
    encoder_stats = Signal(ProgressStats)         # thống kê đại diện (kênh bất kỳ)
    channel_changed = Signal(str, object, str)    # id, RelayState, message
    log_line = Signal(str)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg: StreamConfig | None = None
        self._state = StreamState.IDLE
        self._streams: dict[str, _ChannelStream] = {}
        self._concat_file: str | None = None
        self._user_stopping = False

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (StreamState.STARTING, StreamState.RUNNING)

    def relay_state(self, channel_id: str) -> RelayState:
        s = self._streams.get(channel_id)
        return s.state if s else RelayState.STOPPED

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
        self._user_stopping = False
        try:
            self._concat_file = playlist_manager.write_concat_file(cfg.playlist)
        except ValueError as exc:
            self.error.emit(str(exc))
            return

        self._set_state(StreamState.STARTING)
        self.log_line.emit(f"Bắt đầu {len(cfg.active_channels())} kênh "
                           f"(preset {cfg.preset_key}, encoder {cfg.encoder_key}).")
        self._compute_total_duration_async(cfg.playlist)

        self._streams.clear()
        for ch in cfg.active_channels():
            self._create_stream(ch, fresh=True)

    def stop(self) -> None:
        if self._state == StreamState.IDLE:
            return
        self._user_stopping = True
        self._set_state(StreamState.STOPPING)
        self.log_line.emit("Đang dừng tất cả kênh…")
        for s in list(self._streams.values()):
            s.stop_remove()
        self._streams.clear()
        QTimer.singleShot(1200, self._finalize_idle)

    # ---- điều khiển từng kênh (dùng được cả khi đang live) ----
    def add_channel(self, channel: Channel) -> None:
        if self._cfg is not None and channel.id not in {c.id for c in self._cfg.channels}:
            self._cfg.channels.append(channel)
        if self.is_active and channel.is_valid:
            self._create_stream(channel, fresh=True)
            self.log_line.emit(f"Thêm kênh khi đang live: {channel.name or channel.id}")

    def remove_channel(self, channel_id: str) -> None:
        s = self._streams.pop(channel_id, None)
        if s is not None:
            s.stop_remove()
        if self._cfg is not None:
            self._cfg.channels = [c for c in self._cfg.channels if c.id != channel_id]

    def pause_channel(self, channel_id: str) -> None:
        s = self._streams.get(channel_id)
        if s is not None:
            s.pause()
            self.log_line.emit(f"Tạm dừng kênh {s.channel.name or channel_id}")

    def resume_channel(self, channel_id: str) -> None:
        s = self._streams.get(channel_id)
        if s is None and self._cfg is not None and self.is_active:
            ch = next((c for c in self._cfg.channels if c.id == channel_id), None)
            if ch is not None:
                self._create_stream(ch, fresh=True)
                return
        if s is not None:
            s.resume()
            self.log_line.emit(f"Phát tiếp kênh {s.channel.name or channel_id}")

    # -------------------------------------------------------------- nội bộ
    def _create_stream(self, channel: Channel, fresh: bool) -> None:
        if self._cfg is None or self._concat_file is None:
            return
        s = _ChannelStream(channel, self._cfg, self._concat_file, self)
        s.changed.connect(self.channel_changed)
        s.changed.connect(self._on_channel_changed)
        s.log.connect(self.log_line)
        s.stats.connect(self.encoder_stats)
        self._streams[channel.id] = s
        if fresh:
            s.start_fresh()
        else:
            s.resume()

    def _on_channel_changed(self, channel_id: str, state: RelayState, message: str) -> None:
        # Log trạng thái từng kênh.
        name = channel_id
        s = self._streams.get(channel_id)
        if s is not None:
            name = s.channel.name or channel_id
        elif self._cfg is not None:
            ch = next((c for c in self._cfg.channels if c.id == channel_id), None)
            if ch is not None:
                name = ch.name or channel_id
        label = RELAY_STATE_LABEL.get(state, str(state))
        extra = f" ({message})" if message else ""
        self.log_line.emit(f"Kênh '{name}': {label}{extra}")

        # Kênh nào lên LIVE thì tổng thể coi là RUNNING.
        if state == RelayState.LIVE and self._state == StreamState.STARTING:
            self._set_state(StreamState.RUNNING)

    def _compute_total_duration_async(self, playlist: list[str]) -> None:
        def worker(paths: list[str]) -> None:
            total = 0.0
            for p in paths:
                info = playlist_manager.probe(p)
                if info.duration_sec:
                    total += info.duration_sec
            for s in self._streams.values():
                s.total_duration = total
        threading.Thread(target=worker, args=(list(playlist),), daemon=True).start()

    def _finalize_idle(self) -> None:
        self._cleanup_concat()
        self._set_state(StreamState.IDLE)
        self.log_line.emit("Đã dừng.")

    def _cleanup_concat(self) -> None:
        if self._concat_file and os.path.exists(self._concat_file):
            try:
                os.remove(self._concat_file)
            except OSError:
                pass
        self._concat_file = None

    def _set_state(self, state: StreamState) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
