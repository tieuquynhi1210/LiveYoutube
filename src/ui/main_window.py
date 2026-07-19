"""Cửa sổ chính: mỗi luồng live có playlist riêng, thống kê riêng, progress riêng."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .. import __version__
from ..config import store
from ..core import playlist_manager
from ..core.encoder_detector import detect_encoders
from ..core.ffmpeg_locator import ffmpeg_available
from ..core.models import Channel, StreamConfig
from ..core.presets import PRESETS, get_preset
from ..core.stream_controller import (
    RELAY_STATE_LABEL, RelayState, StreamController, StreamState,
)

_AUTO_ENCODER = "__auto__"

_STATE_COLOR = {
    RelayState.LIVE: "#2ecc71", RelayState.STARTING: "orange",
    RelayState.RECONNECTING: "orange", RelayState.PAUSED: "#888",
    RelayState.ERROR: "#e74c3c", RelayState.STOPPED: "#888",
}


def _fmt(sec: float) -> str:
    s = int(sec)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


class EncoderDetectWorker(QThread):
    done = Signal(list)

    def run(self) -> None:
        try:
            encoders = detect_encoders(functional_test=True)
        except Exception:  # noqa: BLE001
            encoders = []
        self.done.emit(encoders)


class ChannelDialog(QDialog):
    def __init__(self, parent=None, name: str = "", key: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Luồng live")
        form = QFormLayout(self)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("VD: Luồng 1")
        self.key_edit = QLineEdit(key)
        self.key_edit.setPlaceholderText("Dán stream key từ YouTube Studio")
        form.addRow("Tên luồng:", self.name_edit)
        form.addRow("Stream key:", self.key_edit)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.resize(460, 120)

    def values(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.key_edit.text().strip()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"LiveYoutube v{__version__} — Phát video lên nhiều kênh YouTube Live")
        self.resize(1180, 780)

        self.controller = StreamController(self)
        self._channels: list[Channel] = []
        self._current_id: str | None = None
        self._detected_encoders: list = []
        self._log_file = None
        self._open_log_file()

        self._build_ui()
        self._connect_controller()
        self._load_config()
        self._start_encoder_detection()
        self._append_log(f"===== LiveYoutube v{__version__} khởi động =====")
        self._append_log("Mỗi luồng có playlist riêng. Thêm luồng → thêm video → bấm Phát. "
                         "Mỗi luồng encode & đẩy RTMP độc lập.")
        if self._log_file is not None:
            self._append_log(f"Log phiên lưu tại: {self._log_path}")

    # ----------------------------------------------------------------- build
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.addWidget(self._build_settings_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_stream_list_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        log_group = QGroupBox("Nhật ký")
        lg = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(130)
        self.log_view.setFont(QFont("Consolas", 9))
        lg.addWidget(self.log_view)
        root.addWidget(log_group)

    def _build_settings_bar(self) -> QWidget:
        box = QGroupBox("Cấu hình chung (áp dụng cho mọi luồng)")
        h = QHBoxLayout(box)
        h.addWidget(QLabel("Chất lượng:"))
        self.preset_combo = QComboBox()
        for p in PRESETS:
            self.preset_combo.addItem(p.label, p.key)
        self.preset_combo.currentIndexChanged.connect(self._push_settings)
        h.addWidget(self.preset_combo)
        h.addWidget(QLabel("Encoder:"))
        self.encoder_combo = QComboBox()
        self.encoder_combo.addItem("Tự động", _AUTO_ENCODER)
        self.encoder_combo.currentIndexChanged.connect(self._push_settings)
        h.addWidget(self.encoder_combo)
        h.addWidget(QLabel("Bitrate (kbps):"))
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(0, 60000)
        self.bitrate_spin.setSingleStep(1000)
        self.bitrate_spin.setSpecialValueText("Theo preset")
        self.bitrate_spin.valueChanged.connect(self._push_settings)
        h.addWidget(self.bitrate_spin)
        self.loop_check = QCheckBox("Lặp 24/7")
        self.loop_check.setChecked(True)
        self.loop_check.stateChanged.connect(self._push_settings)
        h.addWidget(self.loop_check)
        self.restart_check = QCheckBox("Tự nối lại")
        self.restart_check.setChecked(True)
        h.addWidget(self.restart_check)
        h.addStretch(1)
        save_btn = QPushButton("Lưu cấu hình")
        save_btn.clicked.connect(self._save_config)
        h.addWidget(save_btn)
        stop_all_btn = QPushButton("■ Dừng tất cả")
        stop_all_btn.clicked.connect(lambda: self.controller.stop_all())
        h.addWidget(stop_all_btn)
        return box

    def _build_stream_list_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("<b>Luồng live</b>"))
        self.stream_list = QListWidget()
        self.stream_list.currentItemChanged.connect(self._on_stream_selected)
        v.addWidget(self.stream_list, 1)
        row = QHBoxLayout()
        add_btn = QPushButton("➕ Thêm luồng")
        add_btn.clicked.connect(self._on_add_stream)
        del_btn = QPushButton("🗑 Xoá luồng")
        del_btn.clicked.connect(self._on_delete_stream)
        row.addWidget(add_btn)
        row.addWidget(del_btn)
        v.addLayout(row)
        return w

    def _build_detail_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        # Thông tin luồng
        info = QGroupBox("Thông tin luồng")
        form = QFormLayout(info)
        self.name_edit = QLineEdit()
        self.name_edit.textEdited.connect(self._on_name_edited)
        self.key_edit = QLineEdit()
        self.key_edit.textEdited.connect(self._on_key_edited)
        self.ingest_combo = QComboBox()
        self.ingest_combo.addItem("Chính (a.rtmp)", "primary")
        self.ingest_combo.addItem("Dự phòng (b.rtmp)", "backup")
        self.ingest_combo.currentIndexChanged.connect(self._on_ingest_changed)
        form.addRow("Tên:", self.name_edit)
        form.addRow("Stream key:", self.key_edit)
        form.addRow("Ingest ưu tiên:", self.ingest_combo)
        v.addWidget(info)

        # Playlist riêng
        pl = QGroupBox("Playlist của luồng này")
        plv = QVBoxLayout(pl)
        self.playlist_widget = QListWidget()
        self.playlist_widget.setSelectionMode(QListWidget.ExtendedSelection)
        plv.addWidget(self.playlist_widget)
        pb = QHBoxLayout()
        for text, slot in [
            ("Thêm video…", self._on_add_videos),
            ("▲", lambda: self._move_video(-1)),
            ("▼", lambda: self._move_video(1)),
            ("Xoá", self._on_remove_videos),
            ("Xoá hết", self._on_clear_playlist),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            pb.addWidget(b)
        plv.addLayout(pb)
        v.addWidget(pl, 1)

        # Tiến độ
        prog = QGroupBox("Tiến độ phát")
        pv = QVBoxLayout(prog)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        pv.addWidget(self.progress_bar)
        self.progress_label = QLabel("—")
        pv.addWidget(self.progress_label)
        v.addWidget(prog)

        # Thống kê + điều khiển
        ctl = QGroupBox("Thống kê & điều khiển luồng")
        g = QGridLayout(ctl)
        self.stat_state = self._stat(g, 0, 0, "Trạng thái:")
        self.stat_uptime = self._stat(g, 0, 2, "Thời gian:")
        self.stat_fps = self._stat(g, 1, 0, "FPS:")
        self.stat_bitrate = self._stat(g, 1, 2, "Bitrate:")
        self.stat_speed = self._stat(g, 2, 0, "Tốc độ:")
        self.stat_drop = self._stat(g, 2, 2, "Frame rớt:")
        self.stat_ingest = self._stat(g, 3, 0, "Ingest đang dùng:")
        btns = QHBoxLayout()
        self.play_btn = QPushButton("▶  Phát")
        self.play_btn.setStyleSheet("font-weight:bold; padding:8px;")
        self.play_btn.clicked.connect(self._on_play)
        self.pause_btn = QPushButton("⏸  Tạm dừng")
        self.pause_btn.clicked.connect(self._on_pause_resume)
        self.stop_btn = QPushButton("■  Dừng")
        self.stop_btn.clicked.connect(self._on_stop)
        btns.addWidget(self.play_btn)
        btns.addWidget(self.pause_btn)
        btns.addWidget(self.stop_btn)
        g.addLayout(btns, 4, 0, 1, 4)
        v.addWidget(ctl)
        return w

    def _stat(self, grid: QGridLayout, r: int, c: int, label: str) -> QLabel:
        grid.addWidget(QLabel(label), r, c)
        val = QLabel("—")
        val.setStyleSheet("font-weight:bold;")
        grid.addWidget(val, r, c + 1)
        return val

    # ------------------------------------------------------------ controller
    def _connect_controller(self) -> None:
        self.controller.state_changed.connect(self._on_global_state)
        self.controller.channel_changed.connect(self._on_channel_changed)
        self.controller.channel_stats.connect(self._on_channel_stats)
        self.controller.channel_progress.connect(self._on_channel_progress)
        self.controller.log_line.connect(self._append_log)
        self.controller.error.connect(self._on_error)

    def _start_encoder_detection(self) -> None:
        if not ffmpeg_available():
            self._append_log("⚠ Không tìm thấy FFmpeg. Cài FFmpeg hoặc đặt LIVEYT_FFMPEG.")
        self._append_log("Đang dò encoder…")
        self._detect_worker = EncoderDetectWorker(self)
        self._detect_worker.done.connect(self._on_encoders_detected)
        self._detect_worker.start()

    def _on_encoders_detected(self, encoders: list) -> None:
        self._detected_encoders = encoders
        cur = self.encoder_combo.currentData()
        self.encoder_combo.blockSignals(True)
        self.encoder_combo.clear()
        self.encoder_combo.addItem("Tự động", _AUTO_ENCODER)
        for e in encoders:
            self.encoder_combo.addItem(("⚡ " if e.is_hardware else "") + e.label, e.key)
        idx = self.encoder_combo.findData(cur)
        self.encoder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.encoder_combo.blockSignals(False)
        if encoders:
            self._append_log("Encoder: " + ", ".join(e.label for e in encoders)
                             + f" → tự động dùng {encoders[0].label}")
        self._push_settings()

    # ------------------------------------------------------------- settings
    def _collect_settings(self) -> StreamConfig:
        enc = self.encoder_combo.currentData()
        if enc == _AUTO_ENCODER:
            enc = self._detected_encoders[0].key if self._detected_encoders else "libx264"
        return StreamConfig(
            preset_key=self.preset_combo.currentData() or "2160p30",
            encoder_key=enc,
            loop=self.loop_check.isChecked(),
            bitrate_override_kbps=self.bitrate_spin.value() or None,
            auto_restart=self.restart_check.isChecked(),
        )

    def _push_settings(self, *args) -> None:
        self.controller.set_settings(self._collect_settings())

    # ----------------------------------------------------------- stream list
    def _channel_by_id(self, cid: str) -> Channel | None:
        return next((c for c in self._channels if c.id == cid), None)

    def _current_channel(self) -> Channel | None:
        return self._channel_by_id(self._current_id) if self._current_id else None

    def _add_stream_item(self, channel: Channel) -> None:
        item = QListWidgetItem(channel.name or "Luồng")
        item.setData(Qt.UserRole, channel.id)
        self.stream_list.addItem(item)

    def _stream_item(self, cid: str) -> QListWidgetItem | None:
        for i in range(self.stream_list.count()):
            it = self.stream_list.item(i)
            if it.data(Qt.UserRole) == cid:
                return it
        return None

    def _refresh_stream_item(self, cid: str) -> None:
        it = self._stream_item(cid)
        ch = self._channel_by_id(cid)
        if not it or not ch:
            return
        st = self.controller.channel_state(cid)
        it.setText(f"{ch.name or 'Luồng'}  •  {RELAY_STATE_LABEL.get(st, '')}")
        it.setForeground(QColor(_STATE_COLOR.get(st, "#888")))

    def _on_add_stream(self) -> None:
        dlg = ChannelDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, key = dlg.values()
        ch = Channel(name=name or f"Luồng {len(self._channels) + 1}", stream_key=key, playlist=[])
        self._channels.append(ch)
        self._add_stream_item(ch)
        self.stream_list.setCurrentRow(self.stream_list.count() - 1)
        self._save_config()

    def _on_delete_stream(self) -> None:
        ch = self._current_channel()
        if ch is None:
            return
        if self.controller.is_channel_active(ch.id):
            if QMessageBox.question(self, "Xoá luồng", f"Luồng '{ch.name}' đang phát. Dừng và xoá?",
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        self.controller.remove_channel(ch.id)
        self._channels = [c for c in self._channels if c.id != ch.id]
        it = self._stream_item(ch.id)
        if it:
            self.stream_list.takeItem(self.stream_list.row(it))
        self._save_config()

    def _on_stream_selected(self, cur, prev) -> None:
        self._current_id = cur.data(Qt.UserRole) if cur else None
        self._load_detail()

    # ---------------------------------------------------------------- detail
    def _load_detail(self) -> None:
        ch = self._current_channel()
        has = ch is not None
        for wdg in (self.name_edit, self.key_edit, self.ingest_combo, self.playlist_widget,
                    self.play_btn, self.pause_btn, self.stop_btn):
            wdg.setEnabled(has)
        if not has:
            self.name_edit.clear()
            self.key_edit.clear()
            self.playlist_widget.clear()
            self.progress_bar.setValue(0)
            self.progress_label.setText("—")
            self._reset_stats()
            return
        self.name_edit.setText(ch.name)
        self.key_edit.setText(ch.stream_key)
        self.ingest_combo.setEnabled(True)
        self.ingest_combo.blockSignals(True)
        i = self.ingest_combo.findData(ch.ingest)
        self.ingest_combo.setCurrentIndex(i if i >= 0 else 0)
        self.ingest_combo.blockSignals(False)
        self._reload_playlist_widget()
        self._reset_stats()
        self._update_detail_controls()

    def _on_ingest_changed(self, *args) -> None:
        ch = self._current_channel()
        if ch:
            ch.ingest = self.ingest_combo.currentData()
            self._save_config()

    def _reload_playlist_widget(self) -> None:
        ch = self._current_channel()
        self.playlist_widget.clear()
        if not ch:
            return
        for p in ch.playlist:
            info = playlist_manager.probe(p)
            label = f"⚠ {p} ({info.error})" if info.error \
                else f"{p}   [{info.resolution}, {info.duration_hms}]"
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, p)
            it.setToolTip(p)
            self.playlist_widget.addItem(it)

    def _on_name_edited(self, text: str) -> None:
        ch = self._current_channel()
        if ch:
            ch.name = text
            self._refresh_stream_item(ch.id)

    def _on_key_edited(self, text: str) -> None:
        ch = self._current_channel()
        if ch:
            ch.stream_key = text

    def _on_add_videos(self) -> None:
        ch = self._current_channel()
        if not ch:
            return
        exts = " ".join(f"*{e}" for e in sorted(playlist_manager.VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video", "", f"Video ({exts});;Tất cả (*.*)")
        if files:
            ch.playlist.extend(files)
            self._reload_playlist_widget()
            self._save_config()
            self._append_log(f"Luồng '{ch.name}': thêm {len(files)} video.")

    def _move_video(self, delta: int) -> None:
        ch = self._current_channel()
        row = self.playlist_widget.currentRow()
        if not ch or row < 0:
            return
        new = row + delta
        if not (0 <= new < len(ch.playlist)):
            return
        ch.playlist[row], ch.playlist[new] = ch.playlist[new], ch.playlist[row]
        self._reload_playlist_widget()
        self.playlist_widget.setCurrentRow(new)

    def _on_remove_videos(self) -> None:
        ch = self._current_channel()
        if not ch:
            return
        rows = sorted((self.playlist_widget.row(i) for i in self.playlist_widget.selectedItems()),
                      reverse=True)
        for r in rows:
            if 0 <= r < len(ch.playlist):
                del ch.playlist[r]
        self._reload_playlist_widget()
        self._save_config()

    def _on_clear_playlist(self) -> None:
        ch = self._current_channel()
        if ch:
            ch.playlist.clear()
            self._reload_playlist_widget()
            self._save_config()

    # ---------------------------------------------------------------- actions
    def _on_play(self) -> None:
        ch = self._current_channel()
        if not ch:
            return
        if not ch.playlist:
            QMessageBox.warning(self, "Thiếu video", "Luồng này chưa có video. Hãy thêm video trước.")
            return
        if not ch.stream_key.strip():
            QMessageBox.warning(self, "Thiếu stream key", "Luồng này chưa có stream key.")
            return
        self._push_settings()
        self._save_config()
        self.controller.play_channel(ch)

    def _on_pause_resume(self) -> None:
        ch = self._current_channel()
        if not ch:
            return
        st = self.controller.channel_state(ch.id)
        if st in (RelayState.PAUSED, RelayState.STOPPED, RelayState.ERROR):
            self.controller.resume_channel(ch.id)
        else:
            self.controller.pause_channel(ch.id)

    def _on_stop(self) -> None:
        ch = self._current_channel()
        if ch:
            self.controller.stop_channel(ch.id)

    # ------------------------------------------------------------------ slots
    def _on_global_state(self, state: StreamState) -> None:
        pass  # trạng thái tổng không cần hiển thị riêng ở bản này

    def _on_channel_changed(self, cid: str, state: RelayState, msg: str) -> None:
        self._refresh_stream_item(cid)
        if cid == self._current_id:
            self._update_detail_controls()

    def _update_detail_controls(self) -> None:
        ch = self._current_channel()
        if not ch:
            return
        st = self.controller.channel_state(ch.id)
        active = st in (RelayState.STARTING, RelayState.LIVE, RelayState.RECONNECTING)
        paused = st == RelayState.PAUSED
        self.play_btn.setEnabled(not active and not paused)
        self.pause_btn.setEnabled(active or paused)
        self.pause_btn.setText("▶ Phát tiếp" if paused else "⏸ Tạm dừng")
        self.stop_btn.setEnabled(active or paused)
        label = RELAY_STATE_LABEL.get(st, "—")
        self.stat_state.setText(label)
        self.stat_state.setStyleSheet(f"font-weight:bold; color:{_STATE_COLOR.get(st, '#888')};")
        ing = self.controller.active_ingest(ch.id)
        self.stat_ingest.setText("Chính (a.rtmp)" if ing == "primary" else "Dự phòng (b.rtmp)")
        if st in (RelayState.STOPPED, RelayState.ERROR):
            self.progress_bar.setValue(0)
            self.progress_label.setText("—")
            self._reset_stats(keep_state=True)

    def _on_channel_stats(self, cid: str, s) -> None:
        if cid != self._current_id:
            return
        self.stat_uptime.setText(_fmt(s.out_time_sec))
        self.stat_fps.setText(f"{s.fps:.1f}")
        self.stat_bitrate.setText(f"{s.bitrate_kbps / 1000:.1f} Mbps")
        self.stat_speed.setText(f"{s.speed:.2f}×" + ("  ⚠ chậm hơn realtime" if 0 < s.speed < 0.95 else ""))
        rate = s.drop_rate
        self.stat_drop.setText(f"{s.dropped_frames} ({rate:.1f}%)")
        self.stat_drop.setStyleSheet("font-weight:bold; color:%s;" % ("#e74c3c" if rate >= 1.0 else "inherit"))

    def _on_channel_progress(self, cid: str, pos: float, total: float,
                             clip_idx: int, clip_count: int) -> None:
        if cid != self._current_id or total <= 0:
            return
        self.progress_bar.setRange(0, int(total))
        self.progress_bar.setValue(int(pos))
        self.progress_label.setText(
            f"Clip {clip_idx + 1}/{clip_count}  •  {_fmt(pos)} / {_fmt(total)}"
        )

    def _reset_stats(self, keep_state: bool = False) -> None:
        if not keep_state:
            self.stat_state.setText("—")
            self.stat_state.setStyleSheet("font-weight:bold;")
        for lbl in (self.stat_uptime, self.stat_fps, self.stat_bitrate, self.stat_speed,
                    self.stat_drop, self.stat_ingest):
            lbl.setText("—")
        self.stat_drop.setStyleSheet("font-weight:bold;")

    def _on_error(self, msg: str) -> None:
        self._append_log(f"❌ {msg}")
        QMessageBox.critical(self, "Lỗi", msg)

    # ----------------------------------------------------------------- config
    def _load_config(self) -> None:
        cfg = store.load()
        self._channels = cfg.channels
        for ch in self._channels:
            self._add_stream_item(ch)
        idx = self.preset_combo.findData(cfg.preset_key)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self.loop_check.setChecked(cfg.loop)
        self.restart_check.setChecked(cfg.auto_restart)
        if cfg.bitrate_override_kbps:
            self.bitrate_spin.setValue(cfg.bitrate_override_kbps)
        if self._channels:
            self.stream_list.setCurrentRow(0)
        self._push_settings()

    def _save_config(self) -> None:
        s = self._collect_settings()
        s.channels = self._channels
        store.save(s)

    # -------------------------------------------------------------------- log
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

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.controller.is_active:
            if QMessageBox.question(self, "Đang phát", "Có luồng đang chạy. Dừng và thoát?",
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                event.ignore()
                return
            self.controller.stop_all()
        self._save_config()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
        event.accept()
