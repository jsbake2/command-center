#!/usr/bin/env python3
"""services-panel — local services + light system monitor.

Layout:
  ┌────────────┬──────────────────────────────┐
  │ Sidebar    │  Detail pane                 │
  │            │                              │
  │ SYSTEM     │  status pill + uptime        │
  │  Overview  │  big numbers (CPU/RAM/...)   │
  │            │  sparklines                  │
  │ SERVICES   │  controls (Start/Stop/...)   │
  │  Service…  │  log tail (services only)    │
  └────────────┴──────────────────────────────┘
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from PyQt6 import QtCore, QtGui, QtWidgets

import psutil

from services import (
    ActionResult,
    Service,
    ServiceMetrics,
    ServiceStatus,
    State,
    build_catalog,
    fmt_uptime,
)
from widgets import BigNumber, Sparkline, StatusPill, DOT_COLORS, STATE_LABEL, StatusDot


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

QSS = """
* { font-family: 'Inter', 'Segoe UI', 'Cantarell', sans-serif; }

QMainWindow, QWidget#root { background: #0b0d12; color: #e6e8eb; }
QSplitter::handle { background: #0b0d12; }

/* Sidebar */
QListWidget#sidebar {
    background: #0f1218;
    color: #c8ccd5;
    border: none;
    padding: 8px 0;
    outline: 0;
}
QListWidget#sidebar::item {
    padding: 8px 16px;
    border-left: 3px solid transparent;
}
QListWidget#sidebar::item:selected {
    background: #1a1f2b;
    color: #f3f4f6;
    border-left: 3px solid #34d399;
}
QListWidget#sidebar::item:hover:!selected {
    background: #141822;
}
QLabel#sidebarHeader {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    color: #6b7280;
    padding: 16px 16px 4px 16px;
    text-transform: uppercase;
    background: #0f1218;
}
QLabel#sidebarBrand {
    font-size: 13px;
    font-weight: 700;
    color: #f3f4f6;
    padding: 14px 16px 8px 16px;
    background: #0f1218;
}
QLabel#sidebarBrandSub {
    font-size: 10px;
    color: #6b7280;
    padding: 0 16px 12px 16px;
    background: #0f1218;
}

/* Cards / frames */
QFrame#bignum, QFrame#sparkFrame, QFrame#card {
    background: #131722;
    border: 1px solid #1f2533;
    border-radius: 10px;
}
QLabel#bignumTitle {
    font-size: 11px;
    font-weight: 600;
    color: #9ca3af;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}
QLabel#bignumValue {
    font-size: 28px;
    font-weight: 700;
    color: #f3f4f6;
}
QLabel#bignumUnit {
    font-size: 13px;
    color: #9ca3af;
    padding-bottom: 4px;
}
QLabel#bignumSub {
    font-size: 11px;
    color: #6b7280;
}

QLabel#sparkTitle {
    font-size: 11px;
    font-weight: 600;
    color: #9ca3af;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}
QLabel#sparkValue {
    font-size: 13px;
    color: #e6e8eb;
    font-weight: 600;
}
QLabel#sparkSub {
    font-size: 10px;
    color: #6b7280;
}

QLabel#pillLabel {
    font-size: 13px;
    color: #e6e8eb;
    font-weight: 500;
}

QLabel#serviceTitle {
    font-size: 22px;
    font-weight: 700;
    color: #f3f4f6;
}
QLabel#serviceSubtitle {
    font-size: 12px;
    color: #9ca3af;
}
QLabel#sectionLabel {
    font-size: 11px;
    font-weight: 700;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding-top: 12px;
}

QLabel#kvKey { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.6px; }
QLabel#kvVal { font-size: 13px; color: #e6e8eb; }

/* Buttons */
QPushButton {
    background: #1f2533;
    color: #e6e8eb;
    border: 1px solid #2b3242;
    padding: 8px 16px;
    border-radius: 7px;
    font-size: 12px;
    font-weight: 600;
    min-width: 90px;
}
QPushButton:hover { background: #2a3142; border-color: #3a4358; }
QPushButton:pressed { background: #161b27; }
QPushButton:disabled { color: #4b5563; background: #161a22; border-color: #232936; }

QPushButton#startBtn { background: #134e4a; border-color: #115e59; color: #d1fae5; }
QPushButton#startBtn:hover { background: #155e57; }
QPushButton#startBtn:disabled { background: #14201f; color: #4b5563; border-color: #1f2a29; }

QPushButton#stopBtn { background: #4c1d24; border-color: #5a1f29; color: #fee2e2; }
QPushButton#stopBtn:hover { background: #5b242b; }
QPushButton#stopBtn:disabled { background: #201316; color: #4b5563; border-color: #2a181c; }

QPushButton#disableBtn { background: #1c2433; border-color: #2b3242; color: #9ca3af; }
QPushButton#enableBtn  { background: #1c2433; border-color: #2b3242; color: #9ca3af; }

/* Log */
QPlainTextEdit#log {
    background: #06080d;
    color: #cbd5e1;
    border: 1px solid #1c2230;
    border-radius: 8px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 11px;
    padding: 8px;
}

QStatusBar {
    background: #0a0c12;
    color: #9ca3af;
    border-top: 1px solid #1c2230;
}

QScrollBar:vertical {
    background: transparent; width: 8px; margin: 4px 2px 4px 2px;
}
QScrollBar::handle:vertical {
    background: #2b3242; border-radius: 4px; min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #3a4358; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


# ---------------------------------------------------------------------------
# Background pollers
# ---------------------------------------------------------------------------


@dataclass
class ServiceTick:
    key: str
    status: ServiceStatus
    metrics: ServiceMetrics
    autostart: str


@dataclass
class SystemTick:
    cpu_total: float
    cpu_per_core: list[float]
    mem_used_mb: float
    mem_total_mb: float
    swap_used_mb: float
    swap_total_mb: float
    net_rx_per_s: float
    net_tx_per_s: float
    disk_read_per_s: float
    disk_write_per_s: float
    load_1: float
    boot_time: float


class ServicePoller(QtCore.QObject):
    update = QtCore.pyqtSignal(object)  # ServiceTick

    def __init__(self, services: list[Service], interval_ms: int = 2500):
        super().__init__()
        self._services = services
        self._interval = interval_ms
        self._timer: QtCore.QTimer | None = None

    @QtCore.pyqtSlot()
    def start(self):
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(self._interval)
        QtCore.QTimer.singleShot(0, self.tick)

    @QtCore.pyqtSlot()
    def tick(self):
        for svc in self._services:
            try:
                st = svc.status()
            except Exception as e:  # noqa: BLE001
                st = ServiceStatus(State.UNKNOWN, f"status error: {e}")
            try:
                m = svc.metrics() if st.state == State.RUNNING else ServiceMetrics()
            except Exception:
                m = ServiceMetrics()
            try:
                a = svc.autostart()
            except Exception:
                a = "unknown"
            self.update.emit(ServiceTick(svc.key, st, m, a))


class SystemPoller(QtCore.QObject):
    update = QtCore.pyqtSignal(object)

    def __init__(self, interval_ms: int = 1500):
        super().__init__()
        self._interval = interval_ms
        self._last_net = None       # (rx, tx, t)
        self._last_disk = None      # (r, w, t)
        self._timer: QtCore.QTimer | None = None
        # Prime cpu_percent so the first tick has a meaningful delta.
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    @QtCore.pyqtSlot()
    def start(self):
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(self._interval)
        QtCore.QTimer.singleShot(0, self.tick)

    @QtCore.pyqtSlot()
    def tick(self):
        cpu_total = psutil.cpu_percent(interval=None)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        now = time.monotonic()

        rx_s = tx_s = 0.0
        if self._last_net:
            prx, ptx, pt = self._last_net
            dt = now - pt
            if dt > 0:
                rx_s = max(0, (net.bytes_recv - prx) / dt)
                tx_s = max(0, (net.bytes_sent - ptx) / dt)
        self._last_net = (net.bytes_recv, net.bytes_sent, now)

        r_s = w_s = 0.0
        if disk and self._last_disk:
            pr, pw, pt = self._last_disk
            dt = now - pt
            if dt > 0:
                r_s = max(0, (disk.read_bytes - pr) / dt)
                w_s = max(0, (disk.write_bytes - pw) / dt)
        if disk:
            self._last_disk = (disk.read_bytes, disk.write_bytes, now)

        try:
            load_1, _, _ = os.getloadavg()
        except OSError:
            load_1 = 0.0

        self.update.emit(
            SystemTick(
                cpu_total=cpu_total,
                cpu_per_core=list(cpu_per_core),
                mem_used_mb=vm.used / (1024 * 1024),
                mem_total_mb=vm.total / (1024 * 1024),
                swap_used_mb=sm.used / (1024 * 1024),
                swap_total_mb=sm.total / (1024 * 1024),
                net_rx_per_s=rx_s,
                net_tx_per_s=tx_s,
                disk_read_per_s=r_s,
                disk_write_per_s=w_s,
                load_1=load_1,
                boot_time=psutil.boot_time(),
            )
        )


class ActionWorker(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, str, object)  # key, action, ActionResult

    def __init__(self, service: Service, action: str):
        super().__init__()
        self._service = service
        self._action = action

    @QtCore.pyqtSlot()
    def run(self):
        try:
            fn = {
                "start": self._service.start,
                "stop": self._service.stop,
                "enable": self._service.enable,
                "disable": self._service.disable,
            }.get(self._action)
            result = fn() if fn else ActionResult(False, f"unknown action {self._action}")
        except Exception as e:  # noqa: BLE001
            result = ActionResult(False, f"{self._action} raised: {e}")
        self.finished.emit(self._service.key, self._action, result)


# ---------------------------------------------------------------------------
# Service detail panel
# ---------------------------------------------------------------------------


class ServicePanel(QtWidgets.QWidget):
    action_requested = QtCore.pyqtSignal(str, str)  # key, action

    def __init__(self, service: Service, parent=None):
        super().__init__(parent)
        self.service = service
        self._busy = False
        self._build()
        self._latest_metrics = ServiceMetrics()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        # Header: title + subtitle + status pill
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(2)
        self.title = QtWidgets.QLabel(self.service.label)
        self.title.setObjectName("serviceTitle")
        kill_tag = "  ·  hard kill on stop" if self.service.kill_style == "hard" else ""
        self.subtitle = QtWidgets.QLabel(f"{self.service.category}  ·  {self.service.description}{kill_tag}")
        self.subtitle.setObjectName("serviceSubtitle")
        self.subtitle.setWordWrap(True)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        head.addLayout(col, 1)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(4)
        self.pill = StatusPill()
        right.addWidget(self.pill, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        self.kv_uptime = QtWidgets.QLabel("Uptime —")
        self.kv_uptime.setObjectName("serviceSubtitle")
        self.kv_uptime.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.kv_uptime)
        head.addLayout(right, 0)
        outer.addLayout(head)

        # Big numbers row
        big = QtWidgets.QHBoxLayout()
        big.setSpacing(12)
        self.bn_status = BigNumber("Status", "")
        self.bn_cpu = BigNumber("CPU", "%")
        self.bn_mem = BigNumber("Memory", "MB")
        self.bn_procs = BigNumber("Processes", "")
        for w in (self.bn_status, self.bn_cpu, self.bn_mem, self.bn_procs):
            big.addWidget(w, 1)
        outer.addLayout(big)

        # Sparklines
        spark_row = QtWidgets.QHBoxLayout()
        spark_row.setSpacing(12)
        self.sl_cpu = Sparkline("CPU usage", "%", capacity=120, color="#34d399", max_hint=100.0)
        self.sl_mem = Sparkline("Memory", "MB", capacity=120, color="#60a5fa", max_hint=None,
                                value_formatter=lambda v: f"{v:,.0f} MB")
        spark_row.addWidget(self.sl_cpu, 1)
        spark_row.addWidget(self.sl_mem, 1)
        outer.addLayout(spark_row)

        # Controls
        ctrl_label = QtWidgets.QLabel("Controls", objectName="sectionLabel")
        outer.addWidget(ctrl_label)
        ctrl = QtWidgets.QHBoxLayout()
        ctrl.setSpacing(8)
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_start.setObjectName("startBtn")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_disable = QtWidgets.QPushButton("Disable")
        self.btn_disable.setObjectName("disableBtn")
        self.btn_enable = QtWidgets.QPushButton("Enable")
        self.btn_enable.setObjectName("enableBtn")
        for b, action in (
            (self.btn_start, "start"),
            (self.btn_stop, "stop"),
            (self.btn_enable, "enable"),
            (self.btn_disable, "disable"),
        ):
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=action: self.action_requested.emit(self.service.key, a))
            ctrl.addWidget(b)
        ctrl.addStretch(1)
        self.kv_autostart = QtWidgets.QLabel("Autostart: —")
        self.kv_autostart.setObjectName("kvVal")
        ctrl.addWidget(self.kv_autostart)
        outer.addLayout(ctrl)

        # Log
        log_label = QtWidgets.QLabel("Activity", objectName="sectionLabel")
        outer.addWidget(log_label)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120)
        outer.addWidget(self.log, 1)

    # -- updates ------------------------------------------------------------

    def apply_tick(self, tick: ServiceTick):
        if self._busy:
            return
        self.pill.set_state(tick.status.state, tick.status.detail)
        self.bn_status.set_value(STATE_LABEL.get(tick.status.state, "Unknown"), tick.status.detail or "")
        running = tick.status.state == State.RUNNING

        if running:
            cpu = max(0.0, tick.metrics.cpu_pct)
            mem = max(0.0, tick.metrics.mem_mb)
            self.bn_cpu.set_value(f"{cpu:.1f}", f"{tick.metrics.n_procs} proc")
            self.bn_mem.set_value(f"{mem:,.0f}", "")
            self.bn_procs.set_value(str(tick.metrics.n_procs), "")
            self.sl_cpu.add_value(cpu)
            self.sl_mem.add_value(mem)
            self.kv_uptime.setText(f"Uptime  {fmt_uptime(tick.metrics.started_at)}")
        else:
            self.bn_cpu.set_value("0.0", "")
            self.bn_mem.set_value("0", "")
            self.bn_procs.set_value("0", "")
            self.sl_cpu.add_value(0.0)
            self.sl_mem.add_value(0.0)
            self.kv_uptime.setText("Uptime  —")

        self._latest_metrics = tick.metrics
        self.kv_autostart.setText(f"Autostart: {tick.autostart}")
        # Toggle Enable/Disable visibility based on state.
        if tick.autostart == "enabled":
            self.btn_enable.setVisible(False)
            self.btn_disable.setVisible(True)
        elif tick.autostart == "disabled":
            self.btn_enable.setVisible(True)
            self.btn_disable.setVisible(False)
        else:
            # Unsupported / unknown — show both, but disable.
            self.btn_enable.setVisible(True)
            self.btn_disable.setVisible(True)
            self.btn_enable.setEnabled(tick.autostart != "unsupported")
            self.btn_disable.setEnabled(tick.autostart != "unsupported")

    def set_busy(self, busy: bool, action: str | None = None):
        self._busy = busy
        for b in (self.btn_start, self.btn_stop, self.btn_enable, self.btn_disable):
            b.setEnabled(not busy)
        if busy and action:
            self.pill.set_state(State.PARTIAL, f"{action} in progress")

    def append_log(self, line: str):
        self.log.appendPlainText(line)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())


# ---------------------------------------------------------------------------
# System overview panel
# ---------------------------------------------------------------------------


class SystemPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        head_col = QtWidgets.QVBoxLayout()
        head_col.setSpacing(2)
        title = QtWidgets.QLabel("System", objectName="serviceTitle")
        sub = QtWidgets.QLabel("Live host metrics — updated continuously", objectName="serviceSubtitle")
        head_col.addWidget(title)
        head_col.addWidget(sub)
        outer.addLayout(head_col)

        # Big numbers
        big = QtWidgets.QHBoxLayout()
        big.setSpacing(12)
        self.bn_cpu = BigNumber("CPU", "%")
        self.bn_mem = BigNumber("Memory", "%")
        self.bn_load = BigNumber("Load (1m)", "")
        self.bn_uptime = BigNumber("Uptime", "")
        for w in (self.bn_cpu, self.bn_mem, self.bn_load, self.bn_uptime):
            big.addWidget(w, 1)
        outer.addLayout(big)

        # Charts grid
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(12)
        self.sl_cpu = Sparkline("CPU total", "%", color="#34d399", max_hint=100.0)
        self.sl_mem = Sparkline("Memory used", "%", color="#60a5fa", max_hint=100.0,
                                value_formatter=lambda v: f"{v:.1f}%")
        self.sl_net_rx = Sparkline("Network ↓", " /s", color="#a78bfa", max_hint=None,
                                   value_formatter=_fmt_rate)
        self.sl_net_tx = Sparkline("Network ↑", " /s", color="#f59e0b", max_hint=None,
                                   value_formatter=_fmt_rate)
        self.sl_disk_r = Sparkline("Disk read", " /s", color="#22d3ee", max_hint=None,
                                   value_formatter=_fmt_rate)
        self.sl_disk_w = Sparkline("Disk write", " /s", color="#fb7185", max_hint=None,
                                   value_formatter=_fmt_rate)

        grid.addWidget(self.sl_cpu, 0, 0)
        grid.addWidget(self.sl_mem, 0, 1)
        grid.addWidget(self.sl_net_rx, 1, 0)
        grid.addWidget(self.sl_net_tx, 1, 1)
        grid.addWidget(self.sl_disk_r, 2, 0)
        grid.addWidget(self.sl_disk_w, 2, 1)
        for r in range(3):
            grid.setRowStretch(r, 1)
        for c in range(2):
            grid.setColumnStretch(c, 1)
        outer.addLayout(grid, 1)

    def apply_tick(self, tick: SystemTick):
        self.sl_cpu.add_value(tick.cpu_total)
        mem_pct = (tick.mem_used_mb / tick.mem_total_mb * 100) if tick.mem_total_mb else 0.0
        self.sl_mem.add_value(mem_pct, sub=f"{tick.mem_used_mb/1024:.1f} / {tick.mem_total_mb/1024:.1f} GiB")
        self.sl_net_rx.add_value(tick.net_rx_per_s)
        self.sl_net_tx.add_value(tick.net_tx_per_s)
        self.sl_disk_r.add_value(tick.disk_read_per_s)
        self.sl_disk_w.add_value(tick.disk_write_per_s)

        self.bn_cpu.set_value(f"{tick.cpu_total:.1f}", f"{len(tick.cpu_per_core)} cores")
        self.bn_mem.set_value(f"{mem_pct:.1f}", f"{tick.mem_used_mb/1024:.1f} GiB")
        self.bn_load.set_value(f"{tick.load_1:.2f}", "")
        self.bn_uptime.set_value(_fmt_uptime_short(time.time() - tick.boot_time), "since boot")


def _fmt_rate(v: float) -> str:
    units = [(1024 ** 3, "GB/s"), (1024 ** 2, "MB/s"), (1024, "KB/s"), (1, "B/s")]
    for thr, u in units:
        if v >= thr:
            return f"{v/thr:.1f} {u}"
    return "0 B/s"


def _fmt_uptime_short(secs: float) -> str:
    secs = int(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


SYSTEM_KEY = "__system__"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, services: list[Service]):
        super().__init__()
        self.setWindowTitle("Services Panel")
        self.resize(1180, 760)

        self.services = services
        self.panels: dict[str, ServicePanel] = {}
        self._busy: set[str] = set()
        self._action_threads: dict[str, QtCore.QThread] = {}
        self._cards_by_key: dict[str, QtWidgets.QListWidgetItem] = {}

        self._build_ui()
        self._start_pollers()

    def _build_ui(self):
        root = QtWidgets.QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QtWidgets.QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Sidebar
        side_container = QtWidgets.QWidget()
        side_container.setFixedWidth(280)
        side_container.setStyleSheet("background: #0f1218;")
        side_layout = QtWidgets.QVBoxLayout(side_container)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)

        brand = QtWidgets.QLabel("Services Panel", objectName="sidebarBrand")
        brand_sub = QtWidgets.QLabel("Local control center", objectName="sidebarBrandSub")
        side_layout.addWidget(brand)
        side_layout.addWidget(brand_sub)

        side_layout.addWidget(QtWidgets.QLabel("System", objectName="sidebarHeader"))

        self.sidebar = QtWidgets.QListWidget(objectName="sidebar")
        self.sidebar.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.sidebar.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.currentRowChanged.connect(self._on_row_changed)
        side_layout.addWidget(self.sidebar, 1)
        layout.addWidget(side_container)

        # Right pane
        self.stack = QtWidgets.QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget { background: #0b0d12; }")
        layout.addWidget(self.stack, 1)

        # Build sidebar items + panels
        self._row_to_key: list[str] = []

        # System overview row
        self.system_panel = SystemPanel()
        self.stack.addWidget(self.system_panel)
        item = QtWidgets.QListWidgetItem("Overview")
        item.setData(QtCore.Qt.ItemDataRole.UserRole, SYSTEM_KEY)
        self.sidebar.addItem(item)
        self._row_to_key.append(SYSTEM_KEY)

        # Group services by category
        groups: dict[str, list[Service]] = defaultdict(list)
        for svc in self.services:
            groups[svc.category].append(svc)

        for category, items in groups.items():
            # Section header (non-selectable).
            hdr = QtWidgets.QListWidgetItem(category.upper())
            hdr.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            hdr.setForeground(QtGui.QColor("#6b7280"))
            f = hdr.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() - 1.0)
            hdr.setFont(f)
            hdr.setSizeHint(QtCore.QSize(220, 28))
            hdr.setData(QtCore.Qt.ItemDataRole.UserRole, None)
            self.sidebar.addItem(hdr)
            self._row_to_key.append(None)  # skip slot

            for svc in items:
                row_widget = self._make_sidebar_row(svc)
                row_item = QtWidgets.QListWidgetItem()
                # Force a tall enough row — sizeHint() before layout is unreliable.
                row_item.setSizeHint(QtCore.QSize(260, 60))
                row_item.setData(QtCore.Qt.ItemDataRole.UserRole, svc.key)
                self.sidebar.addItem(row_item)
                self.sidebar.setItemWidget(row_item, row_widget)
                self._row_to_key.append(svc.key)
                self._cards_by_key[svc.key] = row_item

                panel = ServicePanel(svc)
                panel.action_requested.connect(self._on_action_requested)
                self.panels[svc.key] = panel
                self.stack.addWidget(panel)

        # Select first item.
        self.sidebar.setCurrentRow(0)

        self.statusBar().showMessage("Ready")

    def _make_sidebar_row(self, svc: Service) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setMinimumHeight(56)
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(12)
        dot = StatusDot(diameter=10)
        dot.setObjectName(f"sidebarDot_{svc.key}")
        h.addWidget(dot, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        col = QtWidgets.QVBoxLayout()
        col.setSpacing(3)
        col.setContentsMargins(0, 0, 0, 0)
        name = QtWidgets.QLabel(svc.label)
        name.setStyleSheet("color:#f3f4f6; font-size: 14px; font-weight: 600; background: transparent;")
        name.setMinimumHeight(18)
        sub = QtWidgets.QLabel("…")
        sub.setStyleSheet("color:#9ca3af; font-size: 11px; background: transparent;")
        sub.setObjectName(f"sidebarSub_{svc.key}")
        sub.setMinimumHeight(14)
        # Elide overly long subtitle text instead of squishing the row.
        sub.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        col.addWidget(name)
        col.addWidget(sub)
        h.addLayout(col, 1)
        return w

    def _update_sidebar_row(self, key: str, tick: ServiceTick) -> None:
        item = self._cards_by_key.get(key)
        if not item:
            return
        widget = self.sidebar.itemWidget(item)
        if not widget:
            return
        dot = widget.findChild(StatusDot, f"sidebarDot_{key}")
        sub = widget.findChild(QtWidgets.QLabel, f"sidebarSub_{key}")
        if dot:
            dot.set_state(tick.status.state)
        if sub:
            if tick.status.state == State.RUNNING and tick.metrics.cpu_pct >= 0:
                sub.setText(f"{STATE_LABEL[tick.status.state]} · {tick.metrics.cpu_pct:.0f}% · {tick.metrics.mem_mb:,.0f} MB")
            else:
                sub.setText(STATE_LABEL.get(tick.status.state, "Unknown"))

    # -- Polling ------------------------------------------------------------

    def _start_pollers(self):
        # Service poller thread
        self.svc_thread = QtCore.QThread(self)
        self.svc_poller = ServicePoller(self.services, interval_ms=2500)
        self.svc_poller.moveToThread(self.svc_thread)
        self.svc_thread.started.connect(self.svc_poller.start)
        self.svc_poller.update.connect(self._on_service_tick)
        self.svc_thread.start()

        # System poller thread
        self.sys_thread = QtCore.QThread(self)
        self.sys_poller = SystemPoller(interval_ms=1500)
        self.sys_poller.moveToThread(self.sys_thread)
        self.sys_thread.started.connect(self.sys_poller.start)
        self.sys_poller.update.connect(self._on_system_tick)
        self.sys_thread.start()

    @QtCore.pyqtSlot(object)
    def _on_service_tick(self, tick: ServiceTick):
        self._update_sidebar_row(tick.key, tick)
        panel = self.panels.get(tick.key)
        if panel and not panel._busy:
            panel.apply_tick(tick)

    @QtCore.pyqtSlot(object)
    def _on_system_tick(self, tick: SystemTick):
        self.system_panel.apply_tick(tick)

    @QtCore.pyqtSlot(int)
    def _on_row_changed(self, row: int):
        if row < 0 or row >= len(self._row_to_key):
            return
        key = self._row_to_key[row]
        if key is None:
            # Section header — pick the next selectable row.
            self.sidebar.setCurrentRow(row + 1)
            return
        if key == SYSTEM_KEY:
            self.stack.setCurrentWidget(self.system_panel)
        else:
            panel = self.panels.get(key)
            if panel:
                self.stack.setCurrentWidget(panel)

    # -- Actions ------------------------------------------------------------

    @QtCore.pyqtSlot(str, str)
    def _on_action_requested(self, key: str, action: str):
        if key in self._busy:
            return
        svc = next((s for s in self.services if s.key == key), None)
        if svc is None:
            return
        panel = self.panels[key]

        if action == "disable":
            if not _confirm(self, "Disable autostart",
                            f"Disable autostart for “{svc.label}”?\n\n"
                            "It will not start automatically at boot. "
                            "The service will keep running if it's running now."):
                return
        if action == "stop" and svc.kill_style == "hard":
            if not _confirm(self, "Hard kill",
                            f"Force-kill “{svc.label}”? Unsaved progress in the app may be lost."):
                return

        self._busy.add(key)
        panel.set_busy(True, action=action)
        panel.append_log(f"[{ts()}] {action.upper()} requested")

        thread = QtCore.QThread(self)
        worker = ActionWorker(svc, action)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_action_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._action_threads[key] = thread
        thread.start()

    @QtCore.pyqtSlot(str, str, object)
    def _on_action_finished(self, key: str, action: str, result: ActionResult):
        self._busy.discard(key)
        self._action_threads.pop(key, None)
        panel = self.panels.get(key)
        if panel:
            panel.set_busy(False)
            marker = "OK" if result.ok else "FAIL"
            panel.append_log(f"[{ts()}] {action.upper()} {marker} — {result.message}")
        # Force a quick refresh.
        QtCore.QMetaObject.invokeMethod(self.svc_poller, "tick", QtCore.Qt.ConnectionType.QueuedConnection)
        QtCore.QTimer.singleShot(2500, lambda: QtCore.QMetaObject.invokeMethod(
            self.svc_poller, "tick", QtCore.Qt.ConnectionType.QueuedConnection))

    # -- shutdown -----------------------------------------------------------

    def closeEvent(self, ev):  # noqa: N802
        for t in (getattr(self, "svc_thread", None), getattr(self, "sys_thread", None)):
            if t is None:
                continue
            try:
                t.quit()
                t.wait(1500)
            except Exception:
                pass
        super().closeEvent(ev)


def _confirm(parent, title: str, msg: str) -> bool:
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(msg)
    box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
    return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes


def ts() -> str:
    return time.strftime("%H:%M:%S")


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("services-panel")
    # Lets the Wayland compositor (COSMIC) associate this window with the
    # services-panel.desktop launcher entry so the icon in the dock and the
    # pinned-launcher behaviour work correctly.
    app.setDesktopFileName("services-panel")
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services-panel.svg")
    if os.path.exists(icon_path):
        app.setWindowIcon(QtGui.QIcon(icon_path))
    app.setStyleSheet(QSS)

    services = build_catalog()
    win = MainWindow(services)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
