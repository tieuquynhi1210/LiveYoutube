"""Cửa sổ chính của ứng dụng LiveYoutube."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import __version__
from ..config import store
from ..core import playlist_manager
from ..core.encoder_detector import detect_encoders
from ..core.ffmpeg_locator import ffmpeg_available
from ..core.ffmpeg_parser import ProgressStats
from ..core.models import Channel, StreamConfig
from ..core.presets import PRESETS, get_preset
from ..core.stream_controller import (
    RELAY_STATE_LABEL, RelayState, StreamController, StreamState,
)

_AUTO_ENCODER = "__auto__"
_COL_NAME, _COL_KEY, _COL_STATUS, _COL_ACTION = range(4)


class EncoderDetectWorker(QThread):
    done = Signal(list)

    def run(self) -> None:
        try:
            encoders = detect_encoders(functional_test=True)
        except Exception:  # noqa: BLE001
            encoders = []
        self.done.emit(encoders)


class ChannelDialog(QDialog):
    """Hộp thoại nhập tên + stream key cho một kênh."""

    def __init__(self, parent=None, name: str = "", key: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Kênh YouTube")
        form = QFormLayout(self)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("VD: Kênh chính")
        self.key_edit = QLineEdit(key)
        self.key_edit.setPlaceholderText("Dán stream key từ YouTube Studio")
        form.addRow("Tên kênh:", self.name_edit)
        form.addRow("Stream key:", self.key_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(460, 120)

    def values(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.key_edit.text().strip()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"LiveYoutube v{__version__} — Phát video lên nhiều kênh YouTube Live")
        self.resize(1120, 740)

        self.controller = StreamController(self)
        self._detected_encoders: list = []
        self._log_file = None
        self._open_log_file()

        self._build_ui()
        self._connect_controller()
        self._load_config()
        self._start_encoder_detection()
        self._update_bandwidth_estimate()
        self._apply_state(StreamState.IDLE)
        self._append_log(f"===== LiveYoutube v{__version__} khởi động =====")
        self._append_log("Đa kênh: mỗi kênh encode & đẩy RTMP độc lập → thêm/xoá/tạm dừng/phát "
                         "tiếp từng kênh khi đang live; một kênh lỗi không ảnh hưởng kênh khác.")
        if self._log_file is not None:
            self._append_log(f"Log phiên lưu tại: {self._log_path}")

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
        for text, slot in [
            ("Thêm video…", self._on_add_videos),
            ("▲ Lên", lambda: self._move_playlist_item(-1)),
            ("▼ Xuống", lambda: self._move_playlist_item(1)),
            ("Xóa", self._on_remove_videos),
            ("Xóa hết", self._on_clear_playlist),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            pl_buttons.addWidget(b)
        pl_layout.addLayout(pl_buttons)
        layout.addWidget(pl_group, 3)

        # --- Kênh đích ---
        ch_group = QGroupBox("Kênh YouTube (thêm/xoá/tạm dừng được cả khi đang live)")
        ch_layout = QVBoxLayout(ch_group)
        self.channel_table = QTableWidget(0, 4)
        self.channel_table.setHorizontalHeaderLabels(["Tên kênh", "Stream key", "Trạng thái", "Thao tác"])
        header = self.channel_table.horizontalHeader()
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_KEY, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeToContents)
        self.channel_table.itemChanged.connect(self._update_bandwidth_estimate)
        ch_layout.addWidget(self.channel_table)
        add_btn = QPushButton("➕ Thêm kênh")
        add_btn.clicked.connect(self._on_add_channel_clicked)
        ch_layout.addWidget(add_btn)
        layout.addWidget(ch_group, 2)
        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

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
        self.bitrate_spin.valueChanged.connect(self._update_bandwidth_estimate)
        grid.addWidget(self.bitrate_spin, 2, 1)
        self.loop_check = QCheckBox("Lặp playlist vô hạn (24/7)")
        self.loop_check.setChecked(True)
        grid.addWidget(self.loop_check, 3, 0, 1, 2)
        self.restart_check = QCheckBox("Tự khởi động lại khi mất luồng")
        self.restart_check.setChecked(True)
        grid.addWidget(self.restart_check, 4, 0, 1, 2)
        layout.addWidget(cfg_group)

        self.bandwidth_label = QLabel()
        self.bandwidth_label.setWordWrap(True)
        self.bandwidth_label.setFrameShape(QFrame.StyledPanel)
        self.bandwidth_label.setStyleSheet("padding:8px;")
        layout.addWidget(self.bandwidth_label)

        mon_group = QGroupBox("Giám sát (encoder)")
        mon_grid = QGridLayout(mon_group)
        self.stat_state = self._make_stat(mon_grid, 0, "Trạng thái:")
        self.stat_uptime = self._make_stat(mon_grid, 1, "Thời gian live:")
        self.stat_fps = self._make_stat(mon_grid, 2, "FPS:")
        self.stat_bitrate = self._make_stat(mon_grid, 3, "Bitrate:")
        self.stat_speed = self._make_stat(mon_grid, 4, "Tốc độ (speed):")
        layout.addWidget(mon_group)

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
        self.stop_btn = QPushButton("■  DỪNG TẤT CẢ")
        self.stop_btn.setMinimumWidth(130)
        self.stop_btn.setStyleSheet("padding:10px;")
        self.stop_btn.clicked.connect(self._on_stop)
        layout.addWidget(self.stop_btn)
        return bar

    # ------------------------------------------------------------ controller
    def _connect_controller(self) -> None:
        self.controller.state_changed.connect(self._apply_state)
        self.controller.encoder_stats.connect(self._on_stats)
        self.controller.channel_changed.connect(self._on_channel_changed)
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
        idx = self.encoder_combo.findData(current)
        self.encoder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.encoder_combo.blockSignals(False)
        if encoders:
            self._append_log("Encoder khả dụng: " + ", ".join(e.label for e in encoders))
            best = encoders[0]
            self._append_log(f"→ Tự động sẽ dùng: {best.label}"
                             + ("" if best.is_hardware else " (không có GPU — 4K có thể không kịp)"))
        else:
            self._append_log("⚠ Không dò được encoder nào.")

    # -------------------------------------------------------------- playlist
    def _on_add_videos(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(playlist_manager.VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video", "", f"Video ({exts});;Tất cả (*.*)")
        for f in files:
            self._add_playlist_item(f)
        if files:
            self._append_log(f"Đã thêm {len(files)} video.")

    def _add_playlist_item(self, path: str) -> None:
        info = playlist_manager.probe(path)
        label = f"⚠ {path}  ({info.error})" if info.error \
            else f"{path}   [{info.resolution}, {info.duration_hms}]"
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
        return [self.playlist_widget.item(i).data(Qt.UserRole)
                for i in range(self.playlist_widget.count())]

    # --------------------------------------------------------------- channels
    def _add_channel_row(self, channel: Channel) -> None:
        self.channel_table.blockSignals(True)
        row = self.channel_table.rowCount()
        self.channel_table.insertRow(row)

        name_item = QTableWidgetItem(channel.name)
        name_item.setData(Qt.UserRole, channel.id)
        self.channel_table.setItem(row, _COL_NAME, name_item)
        self.channel_table.setItem(row, _COL_KEY, QTableWidgetItem(channel.stream_key))

        status_item = QTableWidgetItem("—")
        status_item.setFlags(Qt.ItemIsEnabled)
        self.channel_table.setItem(row, _COL_STATUS, status_item)

        # Cụm nút thao tác
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(2, 2, 2, 2)
        hl.setSpacing(4)
        pause_btn = QPushButton("Tạm dừng")
        pause_btn.setProperty("channel_id", channel.id)
        pause_btn.clicked.connect(self._on_pause_resume_clicked)
        remove_btn = QPushButton("Xoá")
        remove_btn.setProperty("channel_id", channel.id)
        remove_btn.clicked.connect(self._on_remove_channel_clicked)
        hl.addWidget(pause_btn)
        hl.addWidget(remove_btn)
        self.channel_table.setCellWidget(row, _COL_ACTION, holder)
        self.channel_table.blockSignals(False)
        self._refresh_action_buttons()

    def _row_by_channel_id(self, channel_id: str) -> int:
        for r in range(self.channel_table.rowCount()):
            item = self.channel_table.item(r, _COL_NAME)
            if item and item.data(Qt.UserRole) == channel_id:
                return r
        return -1

    def _channel_id_at(self, row: int) -> str:
        item = self.channel_table.item(row, _COL_NAME)
        return item.data(Qt.UserRole) if item else ""

    def _pause_button_for(self, channel_id: str) -> QPushButton | None:
        row = self._row_by_channel_id(channel_id)
        if row < 0:
            return None
        holder = self.channel_table.cellWidget(row, _COL_ACTION)
        if holder is None:
            return None
        for b in holder.findChildren(QPushButton):
            if b.text() in ("Tạm dừng", "Phát tiếp"):
                return b
        return None

    def _channels(self) -> list[Channel]:
        result = []
        for r in range(self.channel_table.rowCount()):
            name = self.channel_table.item(r, _COL_NAME)
            key = self.channel_table.item(r, _COL_KEY)
            ch = Channel(
                name=name.text() if name else "",
                stream_key=key.text() if key else "",
                enabled=True,
            )
            if name and name.data(Qt.UserRole):
                ch.id = name.data(Qt.UserRole)
            result.append(ch)
        return result

    def _channel_from_row(self, row: int) -> Channel:
        name = self.channel_table.item(row, _COL_NAME)
        key = self.channel_table.item(row, _COL_KEY)
        ch = Channel(name=name.text() if name else "", stream_key=key.text() if key else "")
        if name and name.data(Qt.UserRole):
            ch.id = name.data(Qt.UserRole)
        return ch

    def _on_add_channel_clicked(self) -> None:
        dlg = ChannelDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, key = dlg.values()
        if not key:
            QMessageBox.warning(self, "Thiếu stream key", "Cần nhập stream key cho kênh.")
            return
        channel = Channel(name=name or "Kênh", stream_key=key)
        self._add_channel_row(channel)
        self._update_bandwidth_estimate()
        if self.controller.is_active:
            self.controller.add_channel(channel)  # chạy ngay khi đang live

    def _on_remove_channel_clicked(self) -> None:
        channel_id = self.sender().property("channel_id")
        row = self._row_by_channel_id(channel_id)
        if row < 0:
            return
        name = self.channel_table.item(row, _COL_NAME)
        if self.controller.is_active:
            reply = QMessageBox.question(
                self, "Xoá kênh",
                f"Xoá kênh '{name.text() if name else ''}' khỏi buổi live?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self.controller.remove_channel(channel_id)
        self.channel_table.removeRow(row)
        self._update_bandwidth_estimate()

    def _on_pause_resume_clicked(self) -> None:
        channel_id = self.sender().property("channel_id")
        if not self.controller.is_active:
            return
        st = self.controller.relay_state(channel_id)
        if st in (RelayState.PAUSED, RelayState.STOPPED, RelayState.ERROR):
            self.controller.resume_channel(channel_id)
        else:
            self.controller.pause_channel(channel_id)

    def _on_channel_changed(self, channel_id: str, state: RelayState, message: str) -> None:
        row = self._row_by_channel_id(channel_id)
        if row < 0:
            return
        text = RELAY_STATE_LABEL.get(state, "—")
        if message:
            text += f" ({message})"
        item = self.channel_table.item(row, _COL_STATUS)
        if item:
            item.setText(text)
            colors = {
                RelayState.LIVE: "#2ecc71", RelayState.PAUSED: "#888",
                RelayState.RECONNECTING: "orange", RelayState.STARTING: "orange",
                RelayState.ERROR: "#e74c3c", RelayState.STOPPED: "#888",
            }
            item.setForeground(QColor(colors.get(state, "#888")))
        btn = self._pause_button_for(channel_id)
        if btn:
            btn.setText("Phát tiếp" if state in (RelayState.PAUSED, RelayState.STOPPED, RelayState.ERROR)
                        else "Tạm dừng")

    def _refresh_action_buttons(self) -> None:
        active = self.controller.is_active
        for r in range(self.channel_table.rowCount()):
            holder = self.channel_table.cellWidget(r, _COL_ACTION)
            if holder is None:
                continue
            for b in holder.findChildren(QPushButton):
                if b.text() in ("Tạm dừng", "Phát tiếp"):
                    b.setEnabled(active)   # tạm dừng/phát tiếp chỉ khi đang live

    # ----------------------------------------------------------------- config
    def _collect_config(self) -> StreamConfig:
        encoder_key = self.encoder_combo.currentData()
        if encoder_key == _AUTO_ENCODER:
            encoder_key = self._detected_encoders[0].key if self._detected_encoders else "libx264"
        return StreamConfig(
            playlist=self._playlist_paths(),
            channels=self._channels(),
            preset_key=self.preset_combo.currentData(),
            encoder_key=encoder_key,
            loop=self.loop_check.isChecked(),
            bitrate_override_kbps=self.bitrate_spin.value() or None,
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
            StreamState.STOPPING: ("orange", "Đang dừng…"),
            StreamState.ERROR: ("#e74c3c", "Lỗi"),
        }
        color, text = colors.get(state, ("gray", "—"))
        self.status_dot.setStyleSheet(f"color:{color}; font-size:18px;")
        self.status_text.setText(text)
        self.stat_state.setText(text)
        active = state in (StreamState.STARTING, StreamState.RUNNING, StreamState.STOPPING)
        self.start_btn.setEnabled(not active)
        self.stop_btn.setEnabled(active)
        if state == StreamState.IDLE:
            # reset cột trạng thái các kênh
            for r in range(self.channel_table.rowCount()):
                it = self.channel_table.item(r, _COL_STATUS)
                if it:
                    it.setText("—")
        self._refresh_action_buttons()

    def _on_stats(self, s: ProgressStats) -> None:
        self.stat_uptime.setText(s.uptime_hms)
        self.stat_fps.setText(f"{s.fps:.1f}")
        self.stat_bitrate.setText(f"{s.bitrate_kbps / 1000:.1f} Mbps")
        self.stat_speed.setText(f"{s.speed:.2f}×" + ("  ⚠ chậm hơn realtime" if 0 < s.speed < 0.95 else ""))

    def _on_error(self, msg: str) -> None:
        self._append_log(f"❌ {msg}")
        QMessageBox.critical(self, "Lỗi", msg)

    def _open_log_file(self) -> None:
        try:
            path = store.config_dir() / "session.log"
            self._log_file = open(path, "a", encoding="utf-8")
            self._log_path = path
        except OSError:
            self._log_file = None

    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        if self._log_file is not None:
            try:
                self._log_file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
                self._log_file.flush()
            except (OSError, ValueError):
                pass

    def _update_bandwidth_estimate(self, *args) -> None:
        preset = get_preset(self.preset_combo.currentData())
        bitrate = self.bitrate_spin.value() or preset.video_bitrate_kbps
        n = sum(1 for c in self._channels() if c.is_valid)
        total_mbps = (bitrate + 128) * max(n, 1) / 1000.0
        self.bandwidth_label.setText(
            f"Ước tính băng thông upload: <b>{n} kênh</b> × ~{(bitrate + 128) / 1000:.1f} Mbps "
            f"= <b>{total_mbps:.0f} Mbps</b><br>"
            f"Nên có mạng upload ≥ <b>{total_mbps * 1.2:.0f} Mbps</b> (đã cộng 20% dự phòng)."
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.controller.is_active:
            reply = QMessageBox.question(self, "Đang phát", "Luồng đang chạy. Dừng và thoát?",
                                         QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.controller.stop()
        self._save_config()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
        event.accept()
