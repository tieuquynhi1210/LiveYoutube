"""Điều khiển tiến trình FFmpeg bằng QProcess (bất đồng bộ, không chặn UI).

Trách nhiệm:
  - Sinh file concat từ playlist, dựng lệnh FFmpeg.
  - Khởi động/giám sát tiến trình; đọc stdout (tiến trình) và stderr (log).
  - Tự khởi động lại khi FFmpeg thoát bất thường (nếu bật auto_restart).
  - Dọn file concat tạm khi dừng.
"""
from __future__ import annotations

import os
import threading
import time
from enum import Enum

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import playlist_manager
from .ffmpeg_command import build_command
from .ffmpeg_parser import ProgressParser, ProgressStats
from .models import StreamConfig

# Số lần tự khởi động lại liên tiếp tối đa trước khi bỏ cuộc.
MAX_RESTARTS = 1000
# Thời gian chờ trước khi thử lại (ms).
RESTART_DELAY_MS = 3000
# Watchdog: nếu quá ngần này giây không thấy tiến triển (FFmpeg treo) -> khởi động lại.
STALL_TIMEOUT_SEC = 20.0
# Chu kỳ kiểm tra treo (ms).
WATCHDOG_INTERVAL_MS = 5000


class StreamState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    ERROR = "error"


class StreamController(QObject):
    state_changed = Signal(StreamState)
    stats_updated = Signal(ProgressStats)
    log_line = Signal(str)               # dòng log/thông báo cho UI
    error = Signal(str)                  # lỗi nghiêm trọng

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._parser = ProgressParser()
        self._stdout_buf = ""
        self._cfg: StreamConfig | None = None
        self._concat_file: str | None = None
        self._state = StreamState.IDLE
        self._user_stopping = False
        self._restart_count = 0

        # Watchdog phát hiện treo: đo lần cuối FFmpeg báo tiến triển.
        self._last_progress_ts = 0.0
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_INTERVAL_MS)
        self._watchdog.timeout.connect(self._check_stall)

        # Phát tiếp từ chỗ dừng: vị trí tua cho lần phát kế (giây) + thời gian
        # phát của tiến trình hiện tại + tổng thời lượng playlist (để tính vòng lặp).
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._total_duration = 0.0

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (
            StreamState.STARTING, StreamState.RUNNING, StreamState.RESTARTING,
        )

    def start(self, cfg: StreamConfig) -> None:
        if self.is_active:
            self.log_line.emit("Đang phát rồi, bỏ qua lệnh Start.")
            return
        self._cfg = cfg
        self._restart_count = 0
        self._user_stopping = False
        # Phát mới từ đầu.
        self._resume_offset = 0.0
        self._last_out_time = 0.0
        self._total_duration = 0.0
        self._compute_total_duration_async(cfg.playlist)
        self._launch()

    def _compute_total_duration_async(self, playlist: list[str]) -> None:
        """Tính tổng thời lượng playlist ở luồng nền (để xử lý tua khi lặp vòng)."""
        def worker(paths: list[str]) -> None:
            total = 0.0
            for p in paths:
                info = playlist_manager.probe(p)
                if info.duration_sec:
                    total += info.duration_sec
            self._total_duration = total  # gán float: đọc phía main thread là an toàn
        threading.Thread(target=worker, args=(list(playlist),), daemon=True).start()

    def stop(self) -> None:
        if not self.is_active and self._state != StreamState.ERROR:
            return
        self._user_stopping = True
        self._set_state(StreamState.STOPPING)
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self.log_line.emit("Đang dừng luồng…")
            # Gửi tín hiệu kết thúc; nếu không dừng sẽ kill sau timeout.
            self._proc.terminate()
            QTimer.singleShot(4000, self._force_kill_if_needed)
        else:
            self._finalize_idle()

    # -------------------------------------------------------------- internal
    def _launch(self) -> None:
        assert self._cfg is not None
        try:
            self._concat_file = playlist_manager.write_concat_file(self._cfg.playlist)
            args = build_command(self._cfg, self._concat_file, self._resume_offset)
        except ValueError as exc:
            self.error.emit(str(exc))
            self._set_state(StreamState.ERROR)
            return

        if self._resume_offset > 0.5:
            m, s = divmod(int(self._resume_offset), 60)
            self.log_line.emit(f"Phát tiếp từ vị trí {m:02d}:{s:02d} trong playlist.")

        program, *proc_args = args
        self._parser = ProgressParser()
        self._stdout_buf = ""

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_proc_error)
        self._proc = proc

        self._set_state(StreamState.STARTING if self._restart_count == 0 else StreamState.RESTARTING)
        n_channels = len(self._cfg.active_channels())
        self.log_line.emit(
            f"Khởi động FFmpeg → {n_channels} kênh, preset {self._cfg.preset_key}, "
            f"encoder {self._cfg.encoder_key}."
        )
        proc.start(program, proc_args)

    def _on_stdout(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "ignore")
        self._stdout_buf += data
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            stats = self._parser.feed_line(line)
            if stats is not None:
                self._last_progress_ts = time.monotonic()  # còn nhận tiến triển = chưa treo
                self._last_out_time = stats.out_time_sec    # vị trí phát của tiến trình này
                if self._state in (StreamState.STARTING, StreamState.RESTARTING):
                    self._set_state(StreamState.RUNNING)
                    self._restart_count = 0  # chạy ổn định thì reset đếm
                self.stats_updated.emit(stats)

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "ignore")
        for line in data.splitlines():
            line = line.strip()
            if line:
                self.log_line.emit(f"[ffmpeg] {line}")

    def _on_proc_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.FailedToStart:
            self.error.emit(
                "Không khởi động được FFmpeg. Kiểm tra FFmpeg đã cài / có trên PATH."
            )
            self._set_state(StreamState.ERROR)
            self._cleanup_concat()

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self.log_line.emit(f"FFmpeg kết thúc (mã {exit_code}).")
        self._cleanup_concat()

        if self._user_stopping:
            self._finalize_idle()
            return

        # Thoát bất thường -> cân nhắc tự khởi động lại.
        cfg = self._cfg
        if cfg is not None and cfg.auto_restart and self._restart_count < MAX_RESTARTS:
            self._restart_count += 1
            self._update_resume_offset(cfg)
            self._set_state(StreamState.RESTARTING)
            self.log_line.emit(
                f"Mất luồng — thử khởi động lại lần {self._restart_count}/{MAX_RESTARTS} "
                f"sau {RESTART_DELAY_MS // 1000}s…"
            )
            QTimer.singleShot(RESTART_DELAY_MS, self._launch)
        else:
            if cfg is not None and cfg.auto_restart:
                self.error.emit(f"Đã thử lại {MAX_RESTARTS} lần nhưng vẫn lỗi. Dừng.")
            self._set_state(StreamState.ERROR if not self._user_stopping else StreamState.IDLE)

    def _update_resume_offset(self, cfg: StreamConfig) -> None:
        """Tính vị trí tua cho lần phát lại = chỗ vừa phát dở.

        Vị trí tuyệt đối = điểm tiến trình vừa rồi bắt đầu + thời gian nó đã phát.
        Nếu playlist lặp thì lấy phần dư theo tổng thời lượng để không tua quá đầu ra.
        Chưa biết tổng thời lượng (probe chưa xong) -> an toàn phát lại từ đầu.
        """
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

    def _force_kill_if_needed(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self.log_line.emit("FFmpeg chưa dừng, buộc kết thúc.")
            self._proc.kill()

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

    def _check_stall(self) -> None:
        """Watchdog: FFmpeg còn sống nhưng ngừng tiến triển quá lâu -> coi như treo."""
        if self._state != StreamState.RUNNING:
            return
        if self._last_progress_ts <= 0:
            return
        idle = time.monotonic() - self._last_progress_ts
        if idle > STALL_TIMEOUT_SEC:
            self._watchdog.stop()
            self.log_line.emit(
                f"⚠ Phát hiện treo: {idle:.0f}s không có tiến triển (thường do mạng chập). "
                f"Kill FFmpeg và khởi động lại…"
            )
            # kill() -> _on_finished chạy nhánh khởi động lại (vì không phải user dừng).
            if self._proc is not None and self._proc.state() != QProcess.NotRunning:
                self._proc.kill()

    def _set_state(self, state: StreamState) -> None:
        if state != self._state:
            self._state = state
            # Bật watchdog khi đang chạy, tắt ở mọi trạng thái khác.
            if state == StreamState.RUNNING:
                self._last_progress_ts = time.monotonic()
                self._watchdog.start()
            else:
                self._watchdog.stop()
            self.state_changed.emit(state)
