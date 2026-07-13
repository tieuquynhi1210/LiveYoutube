"""Cửa sổ chính của ứng dụng LiveYoutube."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..config import store
from ..core import playlist_manager
from ..core.encoder_detector import detect_encoders
from ..core.ffmpeg_locator import ffmpeg_available
from ..core.ffmpeg_parser import ProgressStats
from ..core.models import Channel, StreamConfig
from ..core.presets import PRESETS, get_preset
from ..core.stream_controller import StreamController, StreamState

_AUTO_ENCODER = "__auto__"


class EncoderDetectWorker(QThread):
    """Dò encoder ở luồng nền để không chặn UI lúc khởi động."""
    done = Signal(list)

    def run(self) -> None:
        try:
            encoders = detect_encoders(functional_test=True)
        except Exception:  # noqa: BLE001 - dò lỗi thì trả rỗng, UI vẫn chạy
            encoders = []
        self.done.emit(encoders)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LiveYoutube — Phát video lên nhiều kênh YouTube Live")
        self.resize(1080, 720)

        self.controller = StreamController(self)
        self._detected_encoders: list = []

        self._build_ui()
        self._connect_controller()
        self._load_config()
        self._start_encoder_detection()
        self._update_bandwidth_estimate()
        self._apply_state(StreamState.IDLE)

    # ----------------------------------------------------------------- build
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        root.addWidget(self._build_bottom_bar())

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # --- Playlist ---
        pl_group = QGroupBox("Playlist (phát lần lượt, lặp 24/7)")
        pl_layout = QVBoxLayout(pl_group)
        self.playlist_widget = QListWidget()
        self.playlist_widget.setSelectionMode(QListWidget.ExtendedSelection)
        pl_layout.addWidget(self.playlist_widget)

        pl_buttons = QHBoxLayout()
        btn_add = QPushButton("Thêm video…")
        btn_add.clicked.connect(self._on_add_videos)
        btn_up = QPushButton("▲ Lên")
        btn_up.clicked.connect(lambda: self._move_playlist_item(-1))
        btn_down = QPushButton("▼ Xuống")
        btn_down.clicked.connect(lambda: self._move_playlist_item(1))
        btn_remove = QPushButton("Xóa")
        btn_remove.clicked.connect(self._on_remove_videos)
        btn_clear = QPushButton("Xóa hết")
        btn_clear.clicked.connect(self._on_clear_playlist)
        for b in (btn_add, btn_up, btn_down, btn_remove, btn_clear):
            pl_buttons.addWidget(b)
        pl_layout.addLayout(pl_buttons)
        layout.addWidget(pl_group, 3)

        # --- Kênh đích ---
        ch_group = QGroupBox("Kênh YouTube đích (mỗi dòng một stream key)")
        ch_layout = QVBoxLayout(ch_group)
        self.channel_table = QTableWidget(0, 3)
        self.channel_table.setHorizontalHeaderLabels(["Bật", "Tên kênh", "Stream key"])
        header = self.channel_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.channel_table.itemChanged.connect(self._update_bandwidth_estimate)
        ch_layout.addWidget(self.channel_table)

        ch_buttons = QHBoxLayout()
        btn_add_ch = QPushButton("Thêm kênh")
        btn_add_ch.clicked.connect(lambda: self._add_channel_row(Channel("Kênh mới", "")))
        btn_del_ch = QPushButton("Xóa kênh")
        btn_del_ch.clicked.connect(self._on_remove_channel)
        ch_buttons.addWidget(btn_add_ch)
        ch_buttons.addWidget(btn_del_ch)
        ch_buttons.addStretch(1)
        ch_layout.addLayout(ch_buttons)
        layout.addWidget(ch_group, 2)

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # --- Cấu hình phát ---
        cfg_group = QGroupBox("Cấu hình phát")
        grid = QGridLayout(cfg_group)

        grid.addWidget(QLabel("Chất lượng:"), 0, 0)
        self.preset_combo = QComboBox()
        for p in PRESETS:
            self.preset_combo.addItem(p.label, p.key)
        self.preset_combo.currentIndexChanged.connect(self._update_bandwidth_estimate)
        grid.addWidget(self.preset_combo, 0, 1)

        grid.addWidget(QLabel("Encoder:"), 1, 0)
        self.encoder_combo = QComboBox()
        self.encoder_combo.addItem("Tự động (tốt nhất)", _AUTO_ENCODER)
        grid.addWidget(self.encoder_combo, 1, 1)

        grid.addWidget(QLabel("Bitrate ghi đè (kbps):"), 2, 0)
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(0, 60000)
        self.bitrate_spin.setSingleStep(1000)
        self.bitrate_spin.setSpecialValueText("Theo preset")
        self.bitrate_spin.setValue(0)
        self.bitrate_spin.valueChanged.connect(self._update_bandwidth_estimate)
        grid.addWidget(self.bitrate_spin, 2, 1)

        self.loop_check = QCheckBox("Lặp playlist vô hạn (24/7)")
        self.loop_check.setChecked(True)
        grid.addWidget(self.loop_check, 3, 0, 1, 2)

        self.restart_check = QCheckBox("Tự khởi động lại khi mất luồng")
        self.restart_check.setChecked(True)
        grid.addWidget(self.restart_check, 4, 0, 1, 2)

        layout.addWidget(cfg_group)

        # --- Ước tính băng thông ---
        self.bandwidth_label = QLabel()
        self.bandwidth_label.setWordWrap(True)
        self.bandwidth_label.setFrameShape(QFrame.StyledPanel)
        self.bandwidth_label.setStyleSheet("padding:8px;")
        layout.addWidget(self.bandwidth_label)

        # --- Giám sát ---
        mon_group = QGroupBox("Giám sát")
        mon_grid = QGridLayout(mon_group)
        self.stat_state = self._make_stat(mon_grid, 0, "Trạng thái:")
        self.stat_uptime = self._make_stat(mon_grid, 1, "Thời gian live:")
        self.stat_fps = self._make_stat(mon_grid, 2, "FPS:")
        self.stat_bitrate = self._make_stat(mon_grid, 3, "Bitrate:")
        self.stat_speed = self._make_stat(mon_grid, 4, "Tốc độ (speed):")
        self.stat_dropped = self._make_stat(mon_grid, 5, "Frame rớt:")
        layout.addWidget(mon_group)

        # --- Log ---
        log_group = QGroupBox("Nhật ký")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 1)

        return w

    def _make_stat(self, grid: QGridLayout, row: int, label: str) -> QLabel:
        grid.addWidget(QLabel(label), row, 0)
        value = QLabel("—")
        value.setStyleSheet("font-weight:bold;")
        grid.addWidget(value, row, 1)
        return value

    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color:gray; font-size:18px;")
        layout.addWidget(self.status_dot)
        self.status_text = QLabel("Sẵn sàng")
        layout.addWidget(self.status_text)
        layout.addStretch(1)

        btn_save = QPushButton("Lưu cấu hình")
        btn_save.clicked.connect(self._save_config)
        layout.addWidget(btn_save)

        self.start_btn = QPushButton("▶  BẮT ĐẦU PHÁT")
        self.start_btn.setMinimumWidth(180)
        self.start_btn.setStyleSheet("font-weight:bold; padding:10px;")
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■  DỪNG")
        self.stop_btn.setMinimumWidth(120)
        self.stop_btn.setStyleSheet("padding:10px;")
        self.stop_btn.clicked.connect(self._on_stop)
        layout.addWidget(self.stop_btn)

        return bar

    # ------------------------------------------------------------ controller
    def _connect_controller(self) -> None:
        self.controller.state_changed.connect(self._apply_state)
        self.controller.stats_updated.connect(self._on_stats)
        self.controller.log_line.connect(self._append_log)
        self.controller.error.connect(self._on_error)

    def _start_encoder_detection(self) -> None:
        if not ffmpeg_available():
            self._append_log("⚠ Không tìm thấy FFmpeg. Cài FFmpeg hoặc đặt biến LIVEYT_FFMPEG.")
        self._append_log("Đang dò encoder khả dụng…")
        self._detect_worker = EncoderDetectWorker(self)
        self._detect_worker.done.connect(self._on_encoders_detected)
        self._detect_worker.start()

    def _on_encoders_detected(self, encoders: list) -> None:
        self._detected_encoders = encoders
        current = self.encoder_combo.currentData()
        self.encoder_combo.blockSignals(True)
        self.encoder_combo.clear()
        self.encoder_combo.addItem("Tự động (tốt nhất)", _AUTO_ENCODER)
        for enc in encoders:
            tag = "⚡ " if enc.is_hardware else ""
            self.encoder_combo.addItem(f"{tag}{enc.label}", enc.key)
        # khôi phục lựa chọn nếu còn
        idx = self.encoder_combo.findData(current)
        self.encoder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.encoder_combo.blockSignals(False)

        if encoders:
            names = ", ".join(e.label for e in encoders)
            self._append_log(f"Encoder khả dụng: {names}")
            best = encoders[0]
            self._append_log(
                f"→ Tự động sẽ dùng: {best.label}"
                + ("" if best.is_hardware else " (không có GPU encode — 4K có thể không kịp realtime)")
            )
        else:
            self._append_log("⚠ Không dò được encoder nào.")

    # -------------------------------------------------------------- playlist
    def _on_add_videos(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(playlist_manager.VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(
            self, "Chọn video", "", f"Video ({exts});;Tất cả (*.*)"
        )
        for f in files:
            self._add_playlist_item(f)
        if files:
            self._append_log(f"Đã thêm {len(files)} video.")

    def _add_playlist_item(self, path: str) -> None:
        info = playlist_manager.probe(path)
        if info.error:
            label = f"⚠ {path}  ({info.error})"
        else:
            label = f"{path}   [{info.resolution}, {info.duration_hms}]"
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, path)
        item.setToolTip(path)
        self.playlist_widget.addItem(item)

    def _move_playlist_item(self, delta: int) -> None:
        row = self.playlist_widget.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if not (0 <= new_row < self.playlist_widget.count()):
            return
        item = self.playlist_widget.takeItem(row)
        self.playlist_widget.insertItem(new_row, item)
        self.playlist_widget.setCurrentRow(new_row)

    def _on_remove_videos(self) -> None:
        for item in self.playlist_widget.selectedItems():
            self.playlist_widget.takeItem(self.playlist_widget.row(item))

    def _on_clear_playlist(self) -> None:
        self.playlist_widget.clear()

    def _playlist_paths(self) -> list[str]:
        return [
            self.playlist_widget.item(i).data(Qt.UserRole)
            for i in range(self.playlist_widget.count())
        ]

    # --------------------------------------------------------------- channels
    def _add_channel_row(self, channel: Channel) -> None:
        row = self.channel_table.rowCount()
        self.channel_table.insertRow(row)

        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        check.setCheckState(Qt.Checked if channel.enabled else Qt.Unchecked)
        self.channel_table.setItem(row, 0, check)
        self.channel_table.setItem(row, 1, QTableWidgetItem(channel.name))
        self.channel_table.setItem(row, 2, QTableWidgetItem(channel.stream_key))

    def _on_remove_channel(self) -> None:
        rows = sorted({i.row() for i in self.channel_table.selectedItems()}, reverse=True)
        for r in rows:
            self.channel_table.removeRow(r)
        self._update_bandwidth_estimate()

    def _channels(self) -> list[Channel]:
        result = []
        for r in range(self.channel_table.rowCount()):
            check = self.channel_table.item(r, 0)
            name = self.channel_table.item(r, 1)
            key = self.channel_table.item(r, 2)
            result.append(Channel(
                name=name.text() if name else "",
                stream_key=key.text() if key else "",
                enabled=(check.checkState() == Qt.Checked) if check else True,
            ))
        return result

    # ----------------------------------------------------------------- config
    def _collect_config(self) -> StreamConfig:
        encoder_key = self.encoder_combo.currentData()
        if encoder_key == _AUTO_ENCODER:
            encoder_key = self._detected_encoders[0].key if self._detected_encoders else "libx264"
        override = self.bitrate_spin.value() or None
        return StreamConfig(
            playlist=self._playlist_paths(),
            channels=self._channels(),
            preset_key=self.preset_combo.currentData(),
            encoder_key=encoder_key,
            loop=self.loop_check.isChecked(),
            bitrate_override_kbps=override,
            auto_restart=self.restart_check.isChecked(),
        )

    def _load_config(self) -> None:
        cfg = store.load()
        for p in cfg.playlist:
            self._add_playlist_item(p)
        for ch in cfg.channels:
            self._add_channel_row(ch)
        idx = self.preset_combo.findData(cfg.preset_key)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self.loop_check.setChecked(cfg.loop)
        self.restart_check.setChecked(cfg.auto_restart)
        if cfg.bitrate_override_kbps:
            self.bitrate_spin.setValue(cfg.bitrate_override_kbps)

    def _save_config(self) -> None:
        store.save(self._collect_config())
        self._append_log(f"Đã lưu cấu hình: {store.config_path()}")

    # ----------------------------------------------------------------- actions
    def _on_start(self) -> None:
        cfg = self._collect_config()
        if not cfg.playlist:
            QMessageBox.warning(self, "Thiếu playlist", "Hãy thêm ít nhất một video.")
            return
        if not cfg.active_channels():
            QMessageBox.warning(self, "Thiếu kênh", "Hãy thêm ít nhất một kênh có stream key.")
            return
        self._save_config()
        self.controller.start(cfg)

    def _on_stop(self) -> None:
        self.controller.stop()

    # ------------------------------------------------------------------- slots
    def _apply_state(self, state: StreamState) -> None:
        colors = {
            StreamState.IDLE: ("gray", "Sẵn sàng"),
            StreamState.STARTING: ("orange", "Đang khởi động…"),
            StreamState.RUNNING: ("#2ecc71", "ĐANG LIVE"),
            StreamState.RESTARTING: ("orange", "Đang khởi động lại…"),
            StreamState.STOPPING: ("orange", "Đang dừng…"),
            StreamState.ERROR: ("#e74c3c", "Lỗi"),
        }
        color, text = colors.get(state, ("gray", "—"))
        self.status_dot.setStyleSheet(f"color:{color}; font-size:18px;")
        self.status_text.setText(text)
        self.stat_state.setText(text)

        active = state in (
            StreamState.STARTING, StreamState.RUNNING,
            StreamState.RESTARTING, StreamState.STOPPING,
        )
        self.start_btn.setEnabled(not active)
        self.stop_btn.setEnabled(active)

    def _on_stats(self, s: ProgressStats) -> None:
        self.stat_uptime.setText(s.uptime_hms)
        self.stat_fps.setText(f"{s.fps:.1f}")
        self.stat_bitrate.setText(f"{s.bitrate_kbps / 1000:.1f} Mbps")
        speed_txt = f"{s.speed:.2f}×"
        # speed < ~0.95 nghĩa là encode không kịp realtime -> cảnh báo
        self.stat_speed.setText(speed_txt + ("  ⚠ chậm hơn realtime" if 0 < s.speed < 0.95 else ""))
        self.stat_dropped.setText(str(s.dropped_frames))

    def _on_error(self, msg: str) -> None:
        self._append_log(f"❌ {msg}")
        QMessageBox.critical(self, "Lỗi", msg)

    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _update_bandwidth_estimate(self, *args) -> None:
        preset = get_preset(self.preset_combo.currentData())
        bitrate = self.bitrate_spin.value() or preset.video_bitrate_kbps
        n = len([c for c in self._channels() if c.enabled and c.is_valid])
        total_mbps = (bitrate + 128) * max(n, 1) / 1000.0
        with_headroom = total_mbps * 1.2
        self.bandwidth_label.setText(
            f"Ước tính băng thông upload: <b>{n} kênh</b> × ~{(bitrate + 128) / 1000:.1f} Mbps "
            f"= <b>{total_mbps:.0f} Mbps</b><br>"
            f"Nên có mạng upload ≥ <b>{with_headroom:.0f} Mbps</b> (đã cộng 20% dự phòng)."
        )

    # ------------------------------------------------------------------- close
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.controller.is_active:
            reply = QMessageBox.question(
                self, "Đang phát",
                "Luồng đang chạy. Dừng và thoát?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.controller.stop()
        self._save_config()
        event.accept()
