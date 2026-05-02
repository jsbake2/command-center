#!/usr/bin/env python3
"""services-panel — remote services + remote-host hardware monitor.

The panel is a thin client over the services-panel-api running on the
remote host (default http://10.0.0.16:9090). Set $SERVICES_PANEL_API_URL
to point elsewhere; token is read from ~/.config/services-panel/token.

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
from collections import defaultdict

from PyQt6 import QtCore, QtGui, QtWidgets

from services import (
    ActionResult,
    ApiClient,
    ApiError,
    ServiceMeta,
    ServiceMetrics,
    ServiceStatus,
    ServiceTick,
    State,
    SystemTick,
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
    padding: 0;
    border: none;
    background: transparent;
}
QListWidget#sidebar::item:selected,
QListWidget#sidebar::item:hover {
    background: transparent;
}
QWidget#sidebarRow {
    background: transparent;
    border-left: 3px solid transparent;
}
QWidget#sidebarRow:hover {
    background: #141822;
}
QWidget#sidebarRow[selected="true"] {
    background: #1a1f2b;
    border-left: 3px solid #34d399;
}
QWidget#sidebarRow[selected="true"]:hover {
    background: #1a1f2b;
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
# Background pollers — both just call the API on a timer
# ---------------------------------------------------------------------------


class ServicePoller(QtCore.QObject):
    update = QtCore.pyqtSignal(object)  # list[ServiceTick]
    error = QtCore.pyqtSignal(str)

    def __init__(self, client: ApiClient, interval_ms: int = 2500):
        super().__init__()
        self._client = client
        self._interval = interval_ms
        self._timer: QtCore.QTimer | None = None
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="svc-fetch")

    @QtCore.pyqtSlot()
    def start(self):
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(self._interval)
        QtCore.QTimer.singleShot(0, self.tick)

    @QtCore.pyqtSlot()
    def tick(self):
        fut = self._pool.submit(self._fetch)
        fut.add_done_callback(self._on_done)

    def _fetch(self):
        return self._client.service_ticks()

    def _on_done(self, fut):
        try:
            ticks = fut.result()
        except ApiError as e:
            try:
                self.error.emit(str(e))
            except RuntimeError:
                pass
            return
        except Exception as e:  # noqa: BLE001
            try:
                self.error.emit(f"poller error: {e}")
            except RuntimeError:
                pass
            return
        try:
            self.update.emit(ticks)
        except RuntimeError:
            pass

    def shutdown(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


class SystemPoller(QtCore.QObject):
    update = QtCore.pyqtSignal(object)  # SystemTick
    error = QtCore.pyqtSignal(str)

    def __init__(self, client: ApiClient, interval_ms: int = 1500):
        super().__init__()
        self._client = client
        self._interval = interval_ms
        self._timer: QtCore.QTimer | None = None
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sys-fetch")

    @QtCore.pyqtSlot()
    def start(self):
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(self._interval)
        QtCore.QTimer.singleShot(0, self.tick)

    @QtCore.pyqtSlot()
    def tick(self):
        fut = self._pool.submit(self._client.system)
        fut.add_done_callback(self._on_done)

    def _on_done(self, fut):
        try:
            tick = fut.result()
        except ApiError as e:
            try:
                self.error.emit(str(e))
            except RuntimeError:
                pass
            return
        except Exception as e:  # noqa: BLE001
            try:
                self.error.emit(f"system poll error: {e}")
            except RuntimeError:
                pass
            return
        try:
            self.update.emit(tick)
        except RuntimeError:
            pass

    def shutdown(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


class ActionWorker(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, str, object, object)  # key, action, ActionResult, ServiceTick (post-action)

    def __init__(self, client: ApiClient, key: str, action: str):
        super().__init__()
        self._client = client
        self._key = key
        self._action = action

    @QtCore.pyqtSlot()
    def run(self):
        try:
            result, tick = self._client.action(self._key, self._action)
        except ApiError as e:
            result = ActionResult(False, str(e))
            tick = None
        except Exception as e:  # noqa: BLE001
            result = ActionResult(False, f"{self._action} raised: {e}")
            tick = None
        # If the API didn't return a fresh state, fall back to a synthetic one.
        if tick is None:
            tick = ServiceTick(
                key=self._key,
                status=ServiceStatus(State.UNKNOWN, "no state from API"),
                metrics=ServiceMetrics(),
                autostart="unknown",
            )
        self.finished.emit(self._key, self._action, result, tick)


# ---------------------------------------------------------------------------
# Service detail panel
# ---------------------------------------------------------------------------


class ServicePanel(QtWidgets.QWidget):
    action_requested = QtCore.pyqtSignal(str, str)  # key, action

    def __init__(self, meta: ServiceMeta, parent=None):
        super().__init__(parent)
        self.meta = meta
        # Back-compat: existing code path uses .service in some spots.
        self.service = meta
        self._busy = False
        self._build()
        self._latest_metrics = ServiceMetrics()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(2)
        self.title = QtWidgets.QLabel(self.meta.label)
        self.title.setObjectName("serviceTitle")
        kill_tag = "  ·  hard kill on stop" if self.meta.kill_style == "hard" else ""
        self.subtitle = QtWidgets.QLabel(f"{self.meta.category}  ·  {self.meta.description}{kill_tag}")
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

        big = QtWidgets.QHBoxLayout()
        big.setSpacing(12)
        self.bn_status = BigNumber("Status", "")
        self.bn_cpu = BigNumber("CPU", "%")
        self.bn_mem = BigNumber("Memory", "MB")
        self.bn_procs = BigNumber("Processes", "")
        for w in (self.bn_status, self.bn_cpu, self.bn_mem, self.bn_procs):
            big.addWidget(w, 1)
        outer.addLayout(big)

        spark_row = QtWidgets.QHBoxLayout()
        spark_row.setSpacing(12)
        self.sl_cpu = Sparkline("CPU usage", "%", capacity=120, color="#34d399", max_hint=100.0)
        self.sl_mem = Sparkline("Memory", "MB", capacity=120, color="#60a5fa", max_hint=None,
                                value_formatter=lambda v: f"{v:,.0f} MB")
        spark_row.addWidget(self.sl_cpu, 1)
        spark_row.addWidget(self.sl_mem, 1)
        outer.addLayout(spark_row)

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
            b.clicked.connect(lambda _=False, a=action: self.action_requested.emit(self.meta.key, a))
            ctrl.addWidget(b)
        ctrl.addStretch(1)
        self.kv_autostart = QtWidgets.QLabel("Autostart: —")
        self.kv_autostart.setObjectName("kvVal")
        ctrl.addWidget(self.kv_autostart)
        outer.addLayout(ctrl)

        log_label = QtWidgets.QLabel("Activity", objectName="sectionLabel")
        outer.addWidget(log_label)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120)
        outer.addWidget(self.log, 1)

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
        if tick.autostart == "enabled":
            self.btn_enable.setVisible(False)
            self.btn_disable.setVisible(True)
        elif tick.autostart == "disabled":
            self.btn_enable.setVisible(True)
            self.btn_disable.setVisible(False)
        else:
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
# System overview panel — now showing the REMOTE host's hardware
# ---------------------------------------------------------------------------


class SystemPanel(QtWidgets.QWidget):
    def __init__(self, host_label: str, parent=None):
        super().__init__(parent)
        self._host_label = host_label
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        head_col = QtWidgets.QVBoxLayout()
        head_col.setSpacing(2)
        title = QtWidgets.QLabel(f"Host  ·  {self._host_label}", objectName="serviceTitle")
        sub = QtWidgets.QLabel("Live remote-host metrics — updated continuously", objectName="serviceSubtitle")
        head_col.addWidget(title)
        head_col.addWidget(sub)
        outer.addLayout(head_col)

        self.title_lbl = title
        self.subtitle_lbl = sub

        big = QtWidgets.QHBoxLayout()
        big.setSpacing(12)
        self.bn_cpu = BigNumber("CPU", "%")
        self.bn_mem = BigNumber("Memory", "%")
        self.bn_load = BigNumber("Load (1m)", "")
        self.bn_uptime = BigNumber("Uptime", "")
        for w in (self.bn_cpu, self.bn_mem, self.bn_load, self.bn_uptime):
            big.addWidget(w, 1)
        outer.addLayout(big)

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
        if tick.hostname:
            self.title_lbl.setText(f"Host  ·  {tick.hostname}")
        self.sl_cpu.add_value(tick.cpu_total)
        mem_pct = (tick.mem_used_mb / tick.mem_total_mb * 100) if tick.mem_total_mb else 0.0
        self.sl_mem.add_value(mem_pct, sub=f"{tick.mem_used_mb/1024:.1f} / {tick.mem_total_mb/1024:.1f} GiB")
        self.sl_net_rx.add_value(tick.net_rx_per_s)
        self.sl_net_tx.add_value(tick.net_tx_per_s)
        self.sl_disk_r.add_value(tick.disk_read_per_s)
        self.sl_disk_w.add_value(tick.disk_write_per_s)

        self.bn_cpu.set_value(f"{tick.cpu_total:.1f}", f"{len(tick.cpu_per_core)} cores")
        self.bn_mem.set_value(f"{mem_pct:.1f}", f"{tick.mem_used_mb/1024:.1f} GiB")
        self.bn_load.set_value(f"{tick.load_1:.2f}", f"{tick.load_5:.2f} / {tick.load_15:.2f}")
        if tick.boot_time:
            self.bn_uptime.set_value(_fmt_uptime_short(tick.now - tick.boot_time), "since boot")


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
    def __init__(self, client: ApiClient, services: list[ServiceMeta], host_label: str):
        super().__init__()
        self.setWindowTitle("Services Panel")
        self.resize(1180, 760)

        self.client = client
        self.services = services
        self.host_label = host_label
        self.panels: dict[str, ServicePanel] = {}
        self._busy: set[str] = set()
        self._action_threads: dict[str, tuple[QtCore.QThread, "ActionWorker"]] = {}
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
        brand_sub = QtWidgets.QLabel(self.host_label, objectName="sidebarBrandSub")
        side_layout.addWidget(brand)
        side_layout.addWidget(brand_sub)

        side_layout.addWidget(QtWidgets.QLabel("Host", objectName="sidebarHeader"))

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

        self._row_to_key: list[str | None] = []

        # System overview row
        self.system_panel = SystemPanel(self.host_label)
        self.stack.addWidget(self.system_panel)
        item = QtWidgets.QListWidgetItem()
        item.setSizeHint(QtCore.QSize(260, 40))
        item.setData(QtCore.Qt.ItemDataRole.UserRole, SYSTEM_KEY)
        self.sidebar.addItem(item)
        self.sidebar.setItemWidget(item, self._make_overview_row())
        self._row_to_key.append(SYSTEM_KEY)

        # Group services by category, in catalog order
        groups: dict[str, list[ServiceMeta]] = defaultdict(list)
        for svc in self.services:
            groups[svc.category].append(svc)

        for category, items in groups.items():
            hdr = QtWidgets.QListWidgetItem()
            hdr.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            hdr.setSizeHint(QtCore.QSize(260, 32))
            hdr.setData(QtCore.Qt.ItemDataRole.UserRole, None)
            self.sidebar.addItem(hdr)
            self.sidebar.setItemWidget(hdr, self._make_section_header_row(category))
            self._row_to_key.append(None)

            for svc in items:
                row_widget = self._make_sidebar_row(svc)
                row_item = QtWidgets.QListWidgetItem()
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

        self.sidebar.setCurrentRow(0)
        self.statusBar().showMessage(f"Connected to {self.client.base_url}")

    def _make_overview_row(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setObjectName("sidebarRow")
        w.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(0)
        label = QtWidgets.QLabel("Overview")
        label.setStyleSheet(
            "color:#f3f4f6; font-size: 14px; font-weight: 600; background: transparent;"
        )
        h.addWidget(label, 1)
        return w

    def _make_section_header_row(self, text: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(16, 14, 16, 4)
        h.setSpacing(0)
        label = QtWidgets.QLabel(text.upper())
        label.setStyleSheet(
            "color:#6b7280; font-size: 10px; font-weight: 700; "
            "letter-spacing: 1.5px; background: transparent;"
        )
        h.addWidget(label, 1)
        return w

    def _make_sidebar_row(self, svc: ServiceMeta) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setObjectName("sidebarRow")
        w.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
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
        self.svc_thread = QtCore.QThread(self)
        self.svc_poller = ServicePoller(self.client, interval_ms=2500)
        self.svc_poller.moveToThread(self.svc_thread)
        self.svc_thread.started.connect(self.svc_poller.start)
        self.svc_poller.update.connect(self._on_service_ticks)
        self.svc_poller.error.connect(self._on_api_error)
        self.svc_thread.start()

        self.sys_thread = QtCore.QThread(self)
        self.sys_poller = SystemPoller(self.client, interval_ms=1500)
        self.sys_poller.moveToThread(self.sys_thread)
        self.sys_thread.started.connect(self.sys_poller.start)
        self.sys_poller.update.connect(self._on_system_tick)
        self.sys_poller.error.connect(self._on_api_error)
        self.sys_thread.start()

    @QtCore.pyqtSlot(object)
    def _on_service_ticks(self, ticks):
        for tick in ticks:
            self._update_sidebar_row(tick.key, tick)
            panel = self.panels.get(tick.key)
            if panel and not panel._busy:
                panel.apply_tick(tick)
        self.statusBar().showMessage(f"Connected to {self.client.base_url}  ·  last update {time.strftime('%H:%M:%S')}")

    @QtCore.pyqtSlot(object)
    def _on_system_tick(self, tick: SystemTick):
        self.system_panel.apply_tick(tick)

    @QtCore.pyqtSlot(str)
    def _on_api_error(self, msg: str):
        self.statusBar().showMessage(f"API error: {msg}")

    @QtCore.pyqtSlot(int)
    def _on_row_changed(self, row: int):
        if row < 0 or row >= len(self._row_to_key):
            return
        key = self._row_to_key[row]
        if key is None:
            self.sidebar.setCurrentRow(row + 1)
            return
        if key == SYSTEM_KEY:
            self.stack.setCurrentWidget(self.system_panel)
        else:
            panel = self.panels.get(key)
            if panel:
                self.stack.setCurrentWidget(panel)
        self._refresh_sidebar_selection(row)

    def _refresh_sidebar_selection(self, current_row: int) -> None:
        for r in range(self.sidebar.count()):
            item = self.sidebar.item(r)
            widget = self.sidebar.itemWidget(item)
            if widget is None or widget.objectName() != "sidebarRow":
                continue
            is_selected = "true" if r == current_row else "false"
            if widget.property("selected") != is_selected:
                widget.setProperty("selected", is_selected)
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    # -- Actions ------------------------------------------------------------

    @QtCore.pyqtSlot(str, str)
    def _on_action_requested(self, key: str, action: str):
        if key in self._busy:
            return
        meta = next((s for s in self.services if s.key == key), None)
        if meta is None:
            return
        panel = self.panels[key]

        if action == "disable":
            if not _confirm(self, "Disable autostart",
                            f"Disable autostart for “{meta.label}”?\n\n"
                            "It will not start automatically at boot. "
                            "The service will keep running if it's running now."):
                return
        if action == "stop" and meta.kill_style == "hard":
            if not _confirm(self, "Hard kill",
                            f"Force-kill “{meta.label}”? Unsaved progress in the app may be lost."):
                return

        self._busy.add(key)
        panel.set_busy(True, action=action)
        panel.append_log(f"[{ts()}] {action.upper()} requested")

        thread = QtCore.QThread(self)
        worker = ActionWorker(self.client, key, action)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_action_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        # Keep refs so Python GC doesn't reap the worker before run() fires.
        self._action_threads[key] = (thread, worker)
        thread.start()

    @QtCore.pyqtSlot(str, str, object, object)
    def _on_action_finished(self, key: str, action: str, result: ActionResult, tick: ServiceTick):
        self._busy.discard(key)
        self._action_threads.pop(key, None)
        panel = self.panels.get(key)
        if panel:
            panel.set_busy(False)
            marker = "OK" if result.ok else "FAIL"
            panel.append_log(f"[{ts()}] {action.upper()} {marker} — {result.message}")
            panel.apply_tick(tick)
            self._update_sidebar_row(key, tick)

    # -- shutdown -----------------------------------------------------------

    def closeEvent(self, ev):  # noqa: N802
        try:
            self.svc_poller.shutdown()
        except Exception:
            pass
        try:
            self.sys_poller.shutdown()
        except Exception:
            pass
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


def _bootstrap_with_retry(client: ApiClient, max_attempts: int = 3) -> tuple[list[ServiceMeta], str]:
    """Fetch the service catalog + host label, with friendly errors."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            metas = build_catalog(client)
            health = client.health()
            host = health.get("hostname") or "remote"
            return metas, host
        except ApiError as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise SystemExit(
        f"\nservices-panel could not reach the API at {client.base_url}.\n"
        f"  Error: {last_err}\n\n"
        f"Check that services-panel-api is running on the remote host:\n"
        f"  ssh 10.0.0.16 systemctl status services-panel-api\n"
        f"And that ~/.config/services-panel/token matches the server's token."
    )


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("services-panel")
    app.setDesktopFileName("services-panel")
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services-panel.svg")
    if os.path.exists(icon_path):
        app.setWindowIcon(QtGui.QIcon(icon_path))
    app.setStyleSheet(QSS)

    client = ApiClient()
    services, host = _bootstrap_with_retry(client)
    win = MainWindow(client, services, host)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
