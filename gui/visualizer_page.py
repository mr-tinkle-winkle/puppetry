"""
Input Visualizer: a CPS tester + live view of what's held, measured the
way any other app sees input (daemon -> kernel -> compositor -> this
window). That's the number that matters for "how fast can I click" --
unlike a daemon-side benchmark, it includes everything downstream.

Per-event work is a single integer increment (no timestamps, no
repaint): a sampling timer turns counts into rates. Anything heavier
per event would make the tester itself the bottleneck at high rates.
Note Qt reports a fast second click as a double-click event instead of
a press -- both are counted, or the tester would read half.
"""
from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QRectF, QTimer, Qt
from PySide6.QtGui import QFont, QKeySequence, QPainter
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QWidget

from ui_kit.custom_button import CustomButton
from ui_kit.rounded_rect import rounded_rect_path
from ui_kit.theme import Theme, contrast_text
from widgets import PageBase, dim_label, label_style, section_title, mark_input

WINDOW_S = 1.0     # "current" = events in the last second
SAMPLE_MS = 50


class RateCounter:
    def __init__(self):
        self.count = 0
        self.peak = 0.0
        self.current = 0.0
        self._samples: deque = deque()  # (t, count)
        self._first_t = None

    def reset(self) -> None:
        self.__init__()

    def sample(self, now: float) -> None:
        if self.count and self._first_t is None:
            self._first_t = now
        self._samples.append((now, self.count))
        while self._samples and now - self._samples[0][0] > WINDOW_S:
            self._samples.popleft()
        t0, c0 = self._samples[0]
        span = now - t0
        # Until a full window has elapsed, divide by the real span so the
        # first second isn't under-reported.
        self.current = (self.count - c0) / span if span >= SAMPLE_MS / 1000 else 0.0
        self.peak = max(self.peak, self.current)

    def average(self, now: float) -> float:
        if self._first_t is None or now - self._first_t < 0.05:
            return 0.0
        return self.count / (now - self._first_t)


class CpsArea(QWidget):
    """Click (or focus and press keys) here. Keeps focus so Space etc.
    land here instead of activating buttons."""

    def __init__(self, clicks: RateCounter, keys: RateCounter, held_changed, parent=None):
        super().__init__(parent)
        self.clicks, self.keys = clicks, keys
        self.held_changed = held_changed
        self.held_keys: set[str] = set()
        self.held_buttons: set[str] = set()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(260)
        self.setCursor(Qt.PointingHandCursor)
        self.hint = "Click here, or click once and then press keys"

    def _button_name(self, b) -> str:
        return {Qt.LeftButton: "LMB", Qt.RightButton: "RMB", Qt.MiddleButton: "MMB",
                Qt.BackButton: "MB4", Qt.ForwardButton: "MB5"}.get(b, "button")

    def mousePressEvent(self, e) -> None:
        self.clicks.count += 1
        self.setFocus()
        self.held_buttons.add(self._button_name(e.button()))
        self.held_changed()

    mouseDoubleClickEvent = mousePressEvent

    def mouseReleaseEvent(self, e) -> None:
        self.held_buttons.discard(self._button_name(e.button()))
        self.held_changed()

    def keyPressEvent(self, e) -> None:
        if e.isAutoRepeat():
            return
        self.keys.count += 1
        self.held_keys.add(QKeySequence(e.key()).toString() or str(e.key()))
        self.held_changed()

    def keyReleaseEvent(self, e) -> None:
        if e.isAutoRepeat():
            return
        self.held_keys.discard(QKeySequence(e.key()).toString() or str(e.key()))
        self.held_changed()

    def focusOutEvent(self, e) -> None:
        self.held_keys.clear()
        self.held_buttons.clear()
        self.held_changed()
        super().focusOutEvent(e)

    def paintEvent(self, _e) -> None:
        theme = Theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fill = theme.surface()
        if self.hasFocus():
            fill = fill.lighter(112)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.fillPath(rounded_rect_path(r, theme.corner_radius(12)), fill)
        p.setPen(contrast_text(fill))
        f = QFont(self.font())
        f.setPointSize(f.pointSize() + 4)
        p.setFont(f)
        p.drawText(r, Qt.AlignCenter, self.hint)


class VisualizerPage(PageBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        theme = Theme()
        self.clicks = RateCounter()
        self.keys = RateCounter()

        self.cps_title = section_title("CPS tester")
        mark_input(self.cps_title)   # measures real input as an app receives it
        self.content_layout.addWidget(self.cps_title)
        self.content_layout.addWidget(dim_label(
            "Measures what an app actually receives. Past a certain rate the compositor and the "
            "receiving app become the limit rather than Puppetry -- and if the kernel's per-client "
            "event buffer overflows, events are dropped before any app sees them."))

        grid = QGridLayout()
        self.labels = {}
        big = QFont()
        big.setPointSize(big.pointSize() + 10)
        big.setBold(True)
        for col, head in enumerate(("", "Current (/s)", "Peak (/s)", "Average (/s)", "Total")):
            h = QLabel(head)
            h.setStyleSheet(label_style(theme.text().darker(130)))
            grid.addWidget(h, 0, col)
        for row, name in enumerate(("Clicks", "Key presses"), start=1):
            grid.addWidget(QLabel(name), row, 0)
            for col, key in enumerate(("current", "peak", "avg", "total"), start=1):
                lbl = QLabel("0")
                lbl.setFont(big)
                mark_input(lbl)
                self.labels[(name, key)] = lbl
                grid.addWidget(lbl, row, col)
        self.content_layout.addLayout(grid)

        self.area = CpsArea(self.clicks, self.keys, self._update_held)
        self.content_layout.addWidget(self.area)

        row = QHBoxLayout()
        self.held = QLabel("Held: (nothing)")
        row.addWidget(self.held, stretch=1)
        reset = CustomButton("Reset")
        reset.clicked.connect(self.reset)
        row.addWidget(reset)
        self.content_layout.addLayout(row)
        self.content_layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(SAMPLE_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def reset(self) -> None:
        self.clicks.reset()
        self.keys.reset()
        self._tick()

    def _update_held(self) -> None:
        items = sorted(self.area.held_buttons) + sorted(self.area.held_keys)
        self.held.setText("Held: " + (" + ".join(items) if items else "(nothing)"))

    def _tick(self) -> None:
        now = time.perf_counter()
        for name, c in (("Clicks", self.clicks), ("Key presses", self.keys)):
            c.sample(now)
            self.labels[(name, "current")].setText(f"{c.current:,.0f}")
            self.labels[(name, "peak")].setText(f"{c.peak:,.0f}")
            self.labels[(name, "avg")].setText(f"{c.average(now):,.0f}")
            self.labels[(name, "total")].setText(f"{c.count:,}")
