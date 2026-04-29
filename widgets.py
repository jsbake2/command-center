"""Reusable PyQt6 widgets: status dot/pill, big-number metric, sparkline graph."""

from __future__ import annotations

from collections import deque

from PyQt6 import QtCore, QtGui, QtWidgets

from services import State


DOT_COLORS = {
    State.RUNNING: "#34d399",
    State.STOPPED: "#ef4444",
    State.PARTIAL: "#f59e0b",
    State.UNKNOWN: "#6b7280",
}

STATE_LABEL = {
    State.RUNNING: "Running",
    State.STOPPED: "Stopped",
    State.PARTIAL: "Partial",
    State.UNKNOWN: "Unknown",
}


class StatusDot(QtWidgets.QWidget):
    """Small colored circle (with halo) that represents a service state."""

    def __init__(self, diameter: int = 14, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self._color = QtGui.QColor(DOT_COLORS[State.UNKNOWN])
        self.setFixedSize(diameter + 6, diameter + 6)

    def set_state(self, state: State) -> None:
        self._color = QtGui.QColor(DOT_COLORS.get(state, DOT_COLORS[State.UNKNOWN]))
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        halo = QtGui.QColor(self._color)
        halo.setAlpha(70)
        p.setBrush(halo)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        r = self.rect()
        p.drawEllipse(r)
        p.setBrush(self._color)
        p.drawEllipse(r.adjusted(3, 3, -3, -3))
        p.end()


class StatusPill(QtWidgets.QWidget):
    """Status dot + label, laid out horizontally."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.dot = StatusDot(diameter=12)
        self.label = QtWidgets.QLabel("Unknown")
        self.label.setObjectName("pillLabel")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addStretch(1)

    def set_state(self, state: State, detail: str | None = None) -> None:
        self.dot.set_state(state)
        text = STATE_LABEL.get(state, "Unknown")
        if detail:
            text = f"{text}  ·  {detail}"
        self.label.setText(text)


class BigNumber(QtWidgets.QFrame):
    """Title + big numeric value + unit/sub-text."""

    def __init__(self, title: str, unit: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("bignum")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        self._title = QtWidgets.QLabel(title)
        self._title.setObjectName("bignumTitle")
        layout.addWidget(self._title)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self._value = QtWidgets.QLabel("—")
        self._value.setObjectName("bignumValue")
        self._unit = QtWidgets.QLabel(unit)
        self._unit.setObjectName("bignumUnit")
        row.addWidget(self._value, 0, QtCore.Qt.AlignmentFlag.AlignBaseline)
        row.addWidget(self._unit, 0, QtCore.Qt.AlignmentFlag.AlignBaseline)
        row.addStretch(1)
        layout.addLayout(row)

        self._sub = QtWidgets.QLabel("")
        self._sub.setObjectName("bignumSub")
        layout.addWidget(self._sub)

    def set_value(self, value: str, sub: str = "") -> None:
        self._value.setText(value)
        self._sub.setText(sub)


class Sparkline(QtWidgets.QFrame):
    """Compact chart showing the recent history of a single numeric series.

    Draws an area-filled line graph; auto-scales to (0, max(history) or hint).
    """

    def __init__(
        self,
        title: str,
        unit: str = "",
        capacity: int = 120,
        color: str = "#34d399",
        max_hint: float | None = 100.0,  # None = auto-scale
        value_formatter=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("sparkFrame")
        self._capacity = capacity
        self._values: deque[float] = deque(maxlen=capacity)
        self._color = QtGui.QColor(color)
        self._max_hint = max_hint
        self._formatter = value_formatter or (lambda v: f"{v:.1f}{unit}")
        self._title_text = title
        self._unit = unit

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(2)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(4)
        self._title_lbl = QtWidgets.QLabel(title)
        self._title_lbl.setObjectName("sparkTitle")
        head.addWidget(self._title_lbl)
        head.addStretch(1)
        self._value_lbl = QtWidgets.QLabel("—")
        self._value_lbl.setObjectName("sparkValue")
        head.addWidget(self._value_lbl)
        outer.addLayout(head)

        self._canvas = _SparkCanvas(self)
        outer.addWidget(self._canvas, 1)

        self._sub_lbl = QtWidgets.QLabel("")
        self._sub_lbl.setObjectName("sparkSub")
        outer.addWidget(self._sub_lbl)

    def add_value(self, v: float, sub: str = "") -> None:
        self._values.append(v)
        self._value_lbl.setText(self._formatter(v))
        if sub:
            self._sub_lbl.setText(sub)
        self._canvas.set_data(list(self._values), self._color, self._max_hint)

    def reset(self) -> None:
        self._values.clear()
        self._value_lbl.setText("—")
        self._sub_lbl.setText("")
        self._canvas.set_data([], self._color, self._max_hint)


class _SparkCanvas(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list[float] = []
        self._color = QtGui.QColor("#34d399")
        self._max_hint: float | None = 100.0
        self.setMinimumHeight(60)

    def set_data(self, data: list[float], color: QtGui.QColor, max_hint: float | None) -> None:
        self._data = data
        self._color = color
        self._max_hint = max_hint
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 2, 0, -2)
        # Background grid
        bg = QtGui.QColor("#0b0d12")
        p.fillRect(rect, bg)
        grid_pen = QtGui.QPen(QtGui.QColor("#1c2230"))
        grid_pen.setWidthF(1.0)
        p.setPen(grid_pen)
        for i in range(1, 4):
            y = rect.top() + rect.height() * i / 4
            p.drawLine(rect.left(), int(y), rect.right(), int(y))

        if not self._data:
            p.setPen(QtGui.QColor("#3f4658"))
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, "no data yet")
            p.end()
            return

        cap = max(1, len(self._data))
        max_v = max(self._data)
        if self._max_hint is not None:
            max_v = max(max_v, self._max_hint * 0.05)  # tiny floor
            if max_v < self._max_hint and max_v > 0:
                # auto-grow within hint scale
                max_v = max(max_v, self._max_hint * 0.05)
        else:
            max_v = max(max_v, 1e-6)
        # Round up to a friendly cap.
        scale_max = self._max_hint if self._max_hint and max(self._data) <= self._max_hint else _nice_ceiling(max(max_v, 1e-6))

        n = len(self._data)
        if n == 1:
            xs = [rect.center().x()]
        else:
            step = rect.width() / (cap - 1)
            xs = [rect.right() - (cap - 1 - i) * step for i in range(cap - n, cap)]

        def y_for(v: float) -> float:
            if scale_max <= 0:
                return rect.bottom()
            ratio = max(0.0, min(1.0, v / scale_max))
            return rect.bottom() - ratio * rect.height()

        # Build polygon for fill
        path = QtGui.QPainterPath()
        path.moveTo(xs[0], rect.bottom())
        for x, v in zip(xs, self._data):
            path.lineTo(x, y_for(v))
        path.lineTo(xs[-1], rect.bottom())
        path.closeSubpath()

        # Gradient fill
        fill = QtGui.QLinearGradient(0, rect.top(), 0, rect.bottom())
        c1 = QtGui.QColor(self._color)
        c1.setAlpha(110)
        c2 = QtGui.QColor(self._color)
        c2.setAlpha(0)
        fill.setColorAt(0, c1)
        fill.setColorAt(1, c2)
        p.fillPath(path, fill)

        # Line on top
        line_pen = QtGui.QPen(self._color)
        line_pen.setWidthF(1.6)
        line_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(line_pen)
        for i in range(1, n):
            p.drawLine(QtCore.QPointF(xs[i - 1], y_for(self._data[i - 1])),
                       QtCore.QPointF(xs[i], y_for(self._data[i])))

        # Right-edge dot
        dot_pen = QtGui.QPen(self._color)
        dot_pen.setWidth(0)
        p.setBrush(self._color)
        p.setPen(dot_pen)
        last_x, last_v = xs[-1], self._data[-1]
        p.drawEllipse(QtCore.QPointF(last_x, y_for(last_v)), 2.5, 2.5)

        # Top-right scale label
        p.setPen(QtGui.QColor("#4b5365"))
        font = p.font()
        font.setPointSizeF(font.pointSizeF() - 1.5)
        p.setFont(font)
        p.drawText(rect.adjusted(0, 2, -4, 0), QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignRight,
                   _format_scale(scale_max))
        p.end()


def _nice_ceiling(v: float) -> float:
    """Round v up to a 'nice' max for chart scaling."""
    if v <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(v))
    base = v / (10 ** exp)
    if base <= 1:
        nice = 1
    elif base <= 2:
        nice = 2
    elif base <= 5:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exp)


def _format_scale(v: float) -> str:
    if v >= 1024:
        return f"max {v/1024:.1f}k"
    if v >= 100:
        return f"max {v:.0f}"
    return f"max {v:.1f}"
