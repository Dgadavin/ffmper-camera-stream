#!/usr/bin/env python3
"""
GUI wrapper for the UDP video streaming client.
Embeds the video stream directly in the window (no separate ffplay).

Install:  pip install PyQt6
Usage:    python3 client_gui.py
"""

import json
import sys
import os
import shutil
import subprocess
import threading

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QCheckBox, QPushButton, QDialog, QMessageBox,
    QListWidget, QListWidgetItem, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QFont, QImage, QPixmap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import (
    HeartbeatSender,
    HEARTBEAT_PORT,
)

DEVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")

# Resolution for the decoded frames — ffmpeg scales to this
FRAME_W = 640
FRAME_H = 480
FRAME_SIZE = FRAME_W * FRAME_H * 3  # RGB24


# ── Persistence ──────────────────────────────────────────────────────────────

def load_devices() -> list[dict]:
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_devices(devices: list[dict]):
    with open(DEVICES_FILE, "w") as f:
        json.dump(devices, f, indent=2)


# ── Stream client (embedded video) ──────────────────────────────────────────

class StreamClient:
    """Runs ffmpeg to decode the UDP stream into raw RGB frames piped to stdout."""

    def __init__(self, on_frame, on_status, on_stopped):
        self.on_frame   = on_frame    # callback(QImage)
        self.on_status  = on_status   # callback(str)
        self.on_stopped = on_stopped  # callback()
        self._heartbeat = None
        self._proc = None
        self._running = False

    @property
    def running(self):
        return self._running

    @property
    def heartbeat(self):
        return self._heartbeat

    def start(self, host, port, slow, keepalive, stats):
        self._running = True
        threading.Thread(
            target=self._run, args=(host, port, slow, keepalive, stats), daemon=True,
        ).start()

    def _run(self, host, port, slow, keepalive, stats):
        try:
            if keepalive:
                self._heartbeat = HeartbeatSender(host, HEARTBEAT_PORT, stats=stats)
                self._heartbeat.start()

            self.on_status("Connecting...")

            max_delay = "500000" if slow else "100000"

            # ffmpeg: read UDP MPEG-TS → decode → scale → output raw RGB24 to stdout
            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-fflags", "+discardcorrupt+nobuffer",
                "-flags", "low_delay",
                "-max_delay", max_delay,
                "-probesize", "512k",
                "-analyzeduration", "500000",
                "-i", f"udp://0.0.0.0:{port}?overrun_nonfatal=1&fifo_size=50000000",
                "-vf", f"scale={FRAME_W}:{FRAME_H}",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-an",
                "pipe:1",
            ]

            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            self.on_status("Streaming...")

            while self._running:
                data = self._proc.stdout.read(FRAME_SIZE)
                if not data or len(data) < FRAME_SIZE:
                    break
                img = QImage(data, FRAME_W, FRAME_H, FRAME_W * 3, QImage.Format.Format_RGB888).copy()
                self.on_frame(img)

        except Exception as e:
            self.on_status(f"ERROR: {e}")
        finally:
            self._cleanup()
            self._running = False
            self.on_stopped()

    def stop(self):
        self._running = False
        self._cleanup()

    def _cleanup(self):
        if self._proc:
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.kill()
                self._proc.wait(timeout=3)
            except Exception:
                pass
            self._proc = None
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None


# ── Video display widget ─────────────────────────────────────────────────────

class VideoWidget(QWidget):
    """Widget that displays video frames with an optional stats overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.setStyleSheet("background-color: black;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Video label (fills the widget)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._label)

        # Stats overlay (top-left corner, semi-transparent)
        self._stats_overlay = QLabel(self)
        self._stats_overlay.setFont(QFont("Courier", 12, QFont.Weight.Bold))
        self._stats_overlay.setStyleSheet(
            "color: #00FF00; background-color: rgba(0, 0, 0, 160); padding: 6px 10px; border-radius: 4px;"
        )
        self._stats_overlay.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._stats_overlay.hide()
        self._stats_overlay.move(10, 10)

        self._pixmap = None

    def update_frame(self, image: QImage):
        self._pixmap = QPixmap.fromImage(image)
        self._show_scaled()

    def clear_frame(self):
        self._pixmap = None
        self._label.clear()

    def set_stats(self, text: str):
        if text:
            self._stats_overlay.setText(text)
            self._stats_overlay.adjustSize()
            self._stats_overlay.show()
            self._stats_overlay.raise_()
        else:
            self._stats_overlay.hide()

    def _show_scaled(self):
        if self._pixmap:
            scaled = self._pixmap.scaled(
                self._label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show_scaled()


# ── Add / Edit device dialog ─────────────────────────────────────────────────

class DeviceDialog(QDialog):
    def __init__(self, parent=None, device=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Device" if device else "Add Device")
        self.setFixedSize(360, 420)
        self.result = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(24, 20, 24, 20)

        layout.addWidget(QLabel("Device name:"))
        self.name_input = QLineEdit(device.get("name", "") if device else "")
        self.name_input.setFont(QFont("Helvetica", 14))
        self.name_input.setMinimumHeight(34)
        self.name_input.setPlaceholderText("e.g. Living Room Camera")
        layout.addWidget(self.name_input)

        layout.addWidget(QLabel("Server IP:"))
        self.host_input = QLineEdit(device.get("host", "192.168.0.1") if device else "192.168.0.1")
        self.host_input.setFont(QFont("Helvetica", 14))
        self.host_input.setMinimumHeight(34)
        layout.addWidget(self.host_input)

        layout.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit(str(device.get("port", 5000)) if device else "5000")
        self.port_input.setFont(QFont("Helvetica", 14))
        self.port_input.setMaximumWidth(120)
        self.port_input.setMinimumHeight(34)
        layout.addWidget(self.port_input)

        layout.addSpacing(6)
        layout.addWidget(QLabel("Options:"))

        self.keepalive_cb = QCheckBox("Send keepalive")
        self.keepalive_cb.setChecked(device.get("keepalive", True) if device else True)
        layout.addWidget(self.keepalive_cb)

        self.slow_cb = QCheckBox("Slow network mode")
        self.slow_cb.setChecked(device.get("slow", False) if device else False)
        layout.addWidget(self.slow_cb)

        self.stats_cb = QCheckBox("Show RTT stats")
        self.stats_cb.setChecked(device.get("stats", False) if device else False)
        layout.addWidget(self.stats_cb)

        layout.addSpacing(12)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(40)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setFont(QFont("Helvetica", 14, QFont.Weight.Bold))
        save_btn.setMinimumHeight(40)
        save_btn.setStyleSheet("QPushButton { background-color: #2196F3; color: white; border-radius: 6px; }"
                               "QPushButton:hover { background-color: #1976D2; }")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self.name_input.setFocus()

    def _on_save(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Device name is required.")
            return
        host = self.host_input.text().strip()
        if not host:
            QMessageBox.warning(self, "Error", "Server IP is required.")
            return
        try:
            port = int(self.port_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Error", "Port must be a number.")
            return

        self.result = {
            "name": name,
            "host": host,
            "port": port,
            "keepalive": self.keepalive_cb.isChecked(),
            "slow": self.slow_cb.isChecked(),
            "stats": self.stats_cb.isChecked(),
        }
        self.accept()


# ── Main window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    frame_received = pyqtSignal(QImage)
    status_changed = pyqtSignal(str)
    stream_stopped = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Camera Stream Client")
        self.resize(900, 520)
        self.setMinimumSize(700, 400)

        self._devices = load_devices()
        self._active_device = None

        self._client = StreamClient(
            on_frame=lambda img: self.frame_received.emit(img),
            on_status=lambda s: self.status_changed.emit(s),
            on_stopped=lambda: self.stream_stopped.emit(),
        )

        self.frame_received.connect(self._on_frame)
        self.status_changed.connect(self._on_status)
        self.stream_stopped.connect(self._on_stream_stopped)

        # RTT stats polling timer
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._poll_stats)

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Left panel ──
        left = QFrame()
        left.setFixedWidth(220)
        left.setStyleSheet("QFrame { background-color: #2b2b2b; }")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        header = QLabel("  Devices")
        header.setFont(QFont("Helvetica", 14, QFont.Weight.Bold))
        header.setStyleSheet("color: white; padding: 12px 8px 8px 8px; background-color: #2b2b2b;")
        header.setMinimumHeight(44)
        left_layout.addWidget(header)

        self._list = QListWidget()
        self._list.setStyleSheet("""
            QListWidget {
                background-color: #2b2b2b; color: white; border: none;
                font-size: 13px; outline: none;
            }
            QListWidget::item { padding: 10px 12px; border-bottom: 1px solid #3a3a3a; }
            QListWidget::item:selected { background-color: #3a3a5a; }
            QListWidget::item:hover { background-color: #333344; }
        """)
        self._list.itemDoubleClicked.connect(self._on_device_double_click)
        left_layout.addWidget(self._list)

        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(8, 8, 8, 8)
        btn_bar.setSpacing(6)

        for text, color, hover, slot in [
            ("+ Add", "#2196F3", "#1976D2", self._add_device),
            ("Edit", "#555", "#666", self._edit_device),
            ("Delete", "#c62828", "#e53935", self._delete_device),
        ]:
            btn = QPushButton(text)
            btn.setStyleSheet(f"""
                QPushButton {{ background-color: {color}; color: white; border-radius: 4px; padding: 6px 12px; font-weight: bold; }}
                QPushButton:hover {{ background-color: {hover}; }}
            """)
            btn.clicked.connect(slot)
            btn_bar.addWidget(btn)

        left_layout.addLayout(btn_bar)
        main_layout.addWidget(left)

        # ── Right panel ──
        right = QFrame()
        right.setStyleSheet("QFrame { background-color: #1e1e1e; }")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Video area
        self._video = VideoWidget()
        right_layout.addWidget(self._video)

        # Status bar under video
        status_bar = QFrame()
        status_bar.setFixedHeight(48)
        status_bar.setStyleSheet("background-color: #252525;")
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(16, 0, 16, 0)

        self._right_title = QLabel("No device selected")
        self._right_title.setFont(QFont("Helvetica", 13, QFont.Weight.Bold))
        self._right_title.setStyleSheet("color: white;")
        sb_layout.addWidget(self._right_title)

        self._right_status = QLabel("")
        self._right_status.setFont(QFont("Helvetica", 12))
        self._right_status.setStyleSheet("color: #888;")
        sb_layout.addWidget(self._right_status)

        sb_layout.addStretch()

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setFont(QFont("Helvetica", 11, QFont.Weight.Bold))
        self._disconnect_btn.setFixedHeight(32)
        self._disconnect_btn.setStyleSheet("""
            QPushButton { background-color: #f44336; color: white; border-radius: 4px; padding: 0 16px; }
            QPushButton:hover { background-color: #d32f2f; }
        """)
        self._disconnect_btn.clicked.connect(self._disconnect)
        self._disconnect_btn.hide()
        sb_layout.addWidget(self._disconnect_btn)

        self._exit_btn = QPushButton("Exit")
        self._exit_btn.setFont(QFont("Helvetica", 11, QFont.Weight.Bold))
        self._exit_btn.setFixedHeight(32)
        self._exit_btn.setStyleSheet("""
            QPushButton { background-color: #555; color: white; border-radius: 4px; padding: 0 16px; }
            QPushButton:hover { background-color: #666; }
        """)
        self._exit_btn.clicked.connect(self.close)
        sb_layout.addWidget(self._exit_btn)

        right_layout.addWidget(status_bar)
        main_layout.addWidget(right)

        self._refresh_list()

    # ── Device list ──────────────────────────────────────────────────────────

    def _refresh_list(self):
        self._list.clear()
        for dev in self._devices:
            item = QListWidgetItem(f"  {dev['name']}")
            item.setSizeHint(QSize(0, 40))
            self._list.addItem(item)

    def _add_device(self):
        dlg = DeviceDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result:
            self._devices.append(dlg.result)
            save_devices(self._devices)
            self._refresh_list()

    def _edit_device(self):
        row = self._list.currentRow()
        if row < 0:
            QMessageBox.information(self, "Edit", "Select a device first.")
            return
        dlg = DeviceDialog(self, device=self._devices[row])
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result:
            self._devices[row] = dlg.result
            save_devices(self._devices)
            self._refresh_list()

    def _delete_device(self):
        row = self._list.currentRow()
        if row < 0:
            QMessageBox.information(self, "Delete", "Select a device first.")
            return
        name = self._devices[row]["name"]
        reply = QMessageBox.question(self, "Delete Device", f"Delete \"{name}\"?")
        if reply == QMessageBox.StandardButton.Yes:
            self._devices.pop(row)
            save_devices(self._devices)
            self._refresh_list()

    # ── Connect / Disconnect ─────────────────────────────────────────────────

    def _on_device_double_click(self, item):
        row = self._list.row(item)
        if row < 0:
            return
        if self._client.running:
            QMessageBox.information(self, "Busy", "Disconnect the current stream first.")
            return
        if not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "Error", "ffmpeg not found.\n\nmacOS: brew install ffmpeg\nWindows: ffmpeg.org → add to PATH")
            return

        dev = self._devices[row]
        if dev.get("stats") and not dev.get("keepalive"):
            QMessageBox.warning(self, "Error", "RTT stats require keepalive. Edit the device settings.")
            return

        self._active_device = dev
        self._right_title.setText(dev["name"])
        self._right_status.setText("Connecting...")
        self._right_status.setStyleSheet("color: #FFA726;")
        self._disconnect_btn.show()
        self._video.clear_frame()

        self._client.start(dev["host"], dev["port"], dev.get("slow", False),
                           dev.get("keepalive", True), dev.get("stats", False))

        if dev.get("stats") and dev.get("keepalive"):
            self._stats_timer.start(2000)

    def _disconnect(self):
        self._stats_timer.stop()
        self._video.set_stats("")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setText("Stopping...")
        self._right_status.setText("Disconnecting...")
        threading.Thread(target=self._client.stop, daemon=True).start()

    # ── Callbacks (all via signals → main thread) ────────────────────────────

    def _on_frame(self, image: QImage):
        self._video.update_frame(image)

    def _on_status(self, text):
        self._right_status.setText(text)
        if "Streaming" in text:
            self._right_status.setStyleSheet("color: #4CAF50;")
        elif "ERROR" in text:
            self._right_status.setStyleSheet("color: #f44336;")

    def _poll_stats(self):
        hb = self._client.heartbeat
        if not hb:
            return
        with hb._lock:
            if hb.rtt_last is not None:
                avg = hb._rtt_sum / hb._rtt_count if hb._rtt_count else 0
                self._video.set_stats(
                    f"RTT:  {hb.rtt_last:.0f} ms\n"
                    f"Avg:  {avg:.0f} ms\n"
                    f"Min:  {hb.rtt_min:.0f} ms\n"
                    f"Max:  {hb.rtt_max:.0f} ms\n"
                    f"Pings: {hb._rtt_count}"
                )

    def _on_stream_stopped(self):
        self._stats_timer.stop()
        self._video.set_stats("")
        self._right_title.setText("No device selected")
        self._right_status.setText("Double-click a device to connect")
        self._right_status.setStyleSheet("color: #888;")
        self._disconnect_btn.hide()
        self._disconnect_btn.setEnabled(True)
        self._disconnect_btn.setText("Disconnect")
        self._video.clear_frame()
        self._active_device = None

    def closeEvent(self, event):
        if self._client.running:
            self._client.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
