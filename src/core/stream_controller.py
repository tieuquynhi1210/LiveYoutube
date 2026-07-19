"""Điều phối phát đa luồng: mỗi luồng có PLAYLIST RIÊNG và tiến trình encode riêng.

Mỗi luồng tự đọc playlist của nó -> encode -> đẩy THẲNG RTMP lên kênh YouTube.
Người dùng bấm Phát/Dừng/Tạm dừng cho TỪNG luồng (không tự phát khi thêm luồng).
Mỗi luồng báo tiến độ: đang ở clip nào, phút mấy trên tổng thời lượng.
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
PROGRESS_INTERVAL_MS = 1000


class StreamState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"


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

ACTIVE_STATES = (RelayState.STARTING, RelayState.LIVE, RelayState.RECONNECTING)


class _ChannelStream(QObject):
    """Một luồng = một tiến trình FFmpeg encode & đẩy RTMP, playlist riêng."""

    changed = Signal(str, object, str)                 # id, RelayState, message
    log = Signal(str)
    stats = Signal(str, object)                        # id, ProgressStats
    progress = Signal(str, float, float, int, int)     # id, pos, total, clip_idx, clip_count

    def __init__(self, channel: Channel, settings: StreamConfig,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._channel = channel
        self._settings = settings
        self._concat_file: str | None = None
        self._durations: list[float] = []
        self._total = 0.0

        self._proc: QProcess | None = None
        self._parser = ProgressParser()
        self._buf = ""
        self._state = RelayState.STOPPED
        self._intent = "stop"
        self._restart_count = 0
        self._last_progress_ts = 0.0
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._use_backup = channel.prefers_backup   # ingest đang dùng (tự chuyển khi lag)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_stall)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._emit_progress)

    @property
    def channel(self) -> Channel:
        return self._channel

    @property
    def state(self) -> RelayState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in ACTIVE_STATES

    @property
    def active_ingest(self) -> str:
        return "backup" if self._use_backup else "primary"

    def set_channel(self, channel: Channel) -> None:
        self._channel = channel

    def set_settings(self, settings: StreamConfig) -> None:
        self._settings = settings

    # ------------------------------------------------------------ điều khiển
    def start_fresh(self) -> None:
        self._intent = "run"
        self._restart_count = 0
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._use_backup = self._channel.prefers_backup
        self._prepare_sources()
        self._spawn()
        self._progress_timer.start()

    def resume(self) -> None:
        self._intent = "run"
        self._restart_count = 0
        self._spawn()
        self._progress_timer.start()

    def pause(self) -> None:
        self._intent = "pause"
        self._update_resume_offset()
        self._progress_timer.stop()
        self._kill()
        if self._proc is None or self._proc.state() == QProcess.NotRunning:
            self._set_state(RelayState.PAUSED, "")

    def stop_remove(self) -> None:
        self._intent = "stop"
        self._watchdog.stop()
        self._progress_timer.stop()
        self._kill()
        if self._proc is None or self._proc.state() == QProcess.NotRunning:
            self._set_state(RelayState.STOPPED, "")
        self._cleanup_concat()

    # ---------------------------------------------------------------- nội bộ
    def _prepare_sources(self) -> None:
        self._cleanup_concat()
        try:
            self._concat_file = playlist_manager.write_concat_file(self._channel.playlist)
        except ValueError:
            self._concat_file = None
        # Tính thời lượng từng clip ở nền (cho progress bar).
        paths = list(self._channel.playlist)

        def worker() -> None:
            durs, total = playlist_manager.probe_durations(paths)
            self._durations = durs
            self._total = total
        threading.Thread(target=worker, daemon=True).start()

    def _spawn(self) -> None:
        if not self._channel.playlist:
            self._set_state(RelayState.ERROR, "luồng chưa có video")
            return
        try:
            args = build_channel_command(self._settings, self._concat_file or "",
                                         self._channel, self._resume_offset,
                                         use_backup=self._use_backup)
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
        ingest = "dự phòng" if self._use_backup else "chính"
        parts = [f"ingest {ingest}"]
        if self._resume_offset > 0.5:
            m, s = divmod(int(self._resume_offset), 60)
            parts.append(f"phát tiếp từ {m:02d}:{s:02d}")
        self._set_state(RelayState.STARTING, ", ".join(parts))
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
                self.stats.emit(self._channel.id, st)

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "ignore")
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            # Lọc cảnh báo nhiễu vô hại (spam khi lag/lặp): realtime resync, đọc trễ.
            if "time discontinuity detected" in line or "Resumed reading at pts" in line:
                continue
            self.log.emit(f"[{self._channel.name or 'luồng'}] {line}")

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._watchdog.stop()
        if self._intent == "pause":
            self._set_state(RelayState.PAUSED, "")
            return
        if self._intent == "stop":
            self._progress_timer.stop()
            self._set_state(RelayState.STOPPED, "")
            return

        clean_end = (status == QProcess.NormalExit and code == 0)
        if clean_end and not self._settings.loop:
            self._progress_timer.stop()
            self._set_state(RelayState.STOPPED, "hết playlist")
            return
        if self._restart_count >= MAX_RESTARTS:
            self._progress_timer.stop()
            self._set_state(RelayState.ERROR, "quá số lần thử lại")
            return
        self._restart_count += 1
        if clean_end and self._settings.loop:
            self._resume_offset = 0.0
            self._last_out_time = 0.0
            self._set_state(RelayState.RECONNECTING, "hết playlist — lặp lại")
            QTimer.singleShot(300, self._spawn_if_run)
        else:
            self._update_resume_offset()
            # Lag/rớt -> chuyển sang ingest còn lại (chính <-> dự phòng) khi nối lại.
            self._use_backup = not self._use_backup
            which = "dự phòng (b)" if self._use_backup else "chính (a)"
            self._set_state(
                RelayState.RECONNECTING,
                f"lag/rớt → chuyển ingest {which}, nối lại lần {self._restart_count} "
                f"sau {RESTART_DELAY_MS // 1000}s",
            )
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
            self.log.emit(f"[{self._channel.name or 'luồng'}] treo (nghẽn) → nối lại.")
            self._kill()

    def _emit_progress(self) -> None:
        if self._state != RelayState.LIVE or self._total <= 0:
            return
        pos = (self._resume_offset + self._last_out_time) % self._total
        clip_idx = self._clip_index(pos)
        self.progress.emit(self._channel.id, pos, self._total, clip_idx, len(self._durations))

    def _clip_index(self, pos: float) -> int:
        acc = 0.0
        for i, d in enumerate(self._durations):
            acc += d
            if pos < acc:
                return i
        return max(0, len(self._durations) - 1)

    def _update_resume_offset(self) -> None:
        if len(self._channel.playlist) >= 2:
            self._resume_offset = 0.0
            self._last_out_time = 0.0
            return
        absolute = self._resume_offset + self._last_out_time
        total = self._total
        if total > 1.0 and self._settings.loop:
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

    def _cleanup_concat(self) -> None:
        if self._concat_file and os.path.exists(self._concat_file):
            try:
                os.remove(self._concat_file)
            except OSError:
                pass
        self._concat_file = None

    def _set_state(self, state: RelayState, message: str) -> None:
        self._state = state
        self.changed.emit(self._channel.id, state, message)


class StreamController(QObject):
    state_changed = Signal(StreamState)
    channel_changed = Signal(str, object, str)         # id, RelayState, message
    channel_stats = Signal(str, object)                # id, ProgressStats
    channel_progress = Signal(str, float, float, int, int)  # id, pos, total, clip_idx, clip_count
    log_line = Signal(str)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._streams: dict[str, _ChannelStream] = {}
        self._settings = StreamConfig()
        self._state = StreamState.IDLE

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def is_active(self) -> bool:
        return any(s.is_active for s in self._streams.values())

    def set_settings(self, settings: StreamConfig) -> None:
        self._settings = settings
        for s in self._streams.values():
            s.set_settings(settings)

    def channel_state(self, channel_id: str) -> RelayState:
        s = self._streams.get(channel_id)
        return s.state if s else RelayState.STOPPED

    def is_channel_active(self, channel_id: str) -> bool:
        s = self._streams.get(channel_id)
        return s.is_active if s else False

    def active_ingest(self, channel_id: str) -> str:
        s = self._streams.get(channel_id)
        return s.active_ingest if s else "primary"

    def play_channel(self, channel: Channel) -> None:
        if not channel.playlist:
            self.error.emit(f"Luồng '{channel.name}' chưa có video nào.")
            return
        if not channel.is_valid:
            self.error.emit(f"Luồng '{channel.name}' chưa có stream key.")
            return
        s = self._streams.get(channel.id)
        if s is None:
            s = self._create_stream(channel)
        else:
            s.set_channel(channel)
            s.set_settings(self._settings)
        s.start_fresh()
        self.log_line.emit(f"Phát luồng '{channel.name}' ({len(channel.playlist)} video).")
        self._update_state()

    def stop_channel(self, channel_id: str) -> None:
        s = self._streams.get(channel_id)
        if s is not None:
            s.stop_remove()
            self._update_state()

    def pause_channel(self, channel_id: str) -> None:
        s = self._streams.get(channel_id)
        if s is not None and s.is_active:
            s.pause()
            self.log_line.emit(f"Tạm dừng luồng '{s.channel.name}'.")

    def resume_channel(self, channel_id: str) -> None:
        s = self._streams.get(channel_id)
        if s is not None:
            s.resume()
            self.log_line.emit(f"Phát tiếp luồng '{s.channel.name}'.")
            self._update_state()

    def remove_channel(self, channel_id: str) -> None:
        s = self._streams.pop(channel_id, None)
        if s is not None:
            s.stop_remove()
        self._update_state()

    def stop_all(self) -> None:
        for s in list(self._streams.values()):
            s.stop_remove()
        self._update_state()

    # -------------------------------------------------------------- nội bộ
    def _create_stream(self, channel: Channel) -> _ChannelStream:
        s = _ChannelStream(channel, self._settings, self)
        s.changed.connect(self.channel_changed)
        s.changed.connect(self._on_channel_changed)
        s.log.connect(self.log_line)
        s.stats.connect(self.channel_stats)
        s.progress.connect(self.channel_progress)
        self._streams[channel.id] = s
        return s

    def _on_channel_changed(self, channel_id: str, state: RelayState, message: str) -> None:
        s = self._streams.get(channel_id)
        name = s.channel.name if s else channel_id
        label = RELAY_STATE_LABEL.get(state, str(state))
        extra = f" ({message})" if message else ""
        self.log_line.emit(f"Luồng '{name}': {label}{extra}")
        self._update_state()

    def _update_state(self) -> None:
        new = StreamState.RUNNING if self.is_active else StreamState.IDLE
        if new != self._state:
            self._state = new
            self.state_changed.emit(new)
