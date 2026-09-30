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
from PySide6.QtGui import QFont, QKeySequence, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QWidget

from input_transcript import InputLog
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.rounded_rect import rounded_rect_path
from ui_kit.theme import Theme, contrast_text
from widgets import PageBase, dim_label, label_style, section_title, mark_input, mark_output

WINDOW_S = 1.0     # "current" = events in the last second
SAMPLE_MS = 50


# ---------------------------------------------------------------------------
# On-screen keyboard: (evdev code, KEY_ name, label, width in key units).
# A row is a list; None is a half-key gap.
# ---------------------------------------------------------------------------
def _k(code, name, label=None, w=1.0):
    return (code, name, label if label is not None else name[4:], w)


_LETTERS = {c: n for c, n in zip(
    (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44),
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
_LETTERS = {n: c for c, n in _LETTERS.items()}


def _ltr(ch, w=1.0):
    return _k(_LETTERS[ch], "KEY_" + ch, ch, w)


KEY_ROWS = [
    [_k(1, "KEY_ESC", "Esc"), None] + [_k(59 + i, f"KEY_F{i + 1}", f"F{i + 1}") for i in range(10)]
    + [_k(87, "KEY_F11", "F11"), _k(88, "KEY_F12", "F12")],
    [_k(41, "KEY_GRAVE", "`")] + [_k(2 + i, f"KEY_{(i + 1) % 10}", str((i + 1) % 10)) for i in range(10)]
    + [_k(12, "KEY_MINUS", "-"), _k(13, "KEY_EQUAL", "="), _k(14, "KEY_BACKSPACE", "Bksp", 2.0)],
    [_k(15, "KEY_TAB", "Tab", 1.5)] + [_ltr(c) for c in "QWERTYUIOP"]
    + [_k(26, "KEY_LEFTBRACE", "["), _k(27, "KEY_RIGHTBRACE", "]"), _k(43, "KEY_BACKSLASH", "\\", 1.5)],
    [_k(58, "KEY_CAPSLOCK", "Caps", 1.75)] + [_ltr(c) for c in "ASDFGHJKL"]
    + [_k(39, "KEY_SEMICOLON", ";"), _k(40, "KEY_APOSTROPHE", "'"), _k(28, "KEY_ENTER", "Enter", 2.25)],
    [_k(42, "KEY_LEFTSHIFT", "Shift", 2.25)] + [_ltr(c) for c in "ZXCVBNM"]
    + [_k(51, "KEY_COMMA", ","), _k(52, "KEY_DOT", "."), _k(53, "KEY_SLASH", "/"), _k(54, "KEY_RIGHTSHIFT", "Shift", 2.75)],
    [_k(29, "KEY_LEFTCTRL", "Ctrl", 1.25), _k(125, "KEY_LEFTMETA", "Meta", 1.25), _k(56, "KEY_LEFTALT", "Alt", 1.25),
     _k(57, "KEY_SPACE", "", 6.25), _k(100, "KEY_RIGHTALT", "Alt", 1.25), _k(126, "KEY_RIGHTMETA", "Meta", 1.25),
     _k(127, "KEY_COMPOSE", "Menu", 1.25), _k(97, "KEY_RIGHTCTRL", "Ctrl", 1.25)],
]
# navigation block: (row, column offset in units, key)
NAV_KEYS = [(1, 0, _k(110, "KEY_INSERT", "Ins")), (1, 1, _k(102, "KEY_HOME", "Home")), (1, 2, _k(104, "KEY_PAGEUP", "PgUp")),
            (2, 0, _k(111, "KEY_DELETE", "Del")), (2, 1, _k(107, "KEY_END", "End")), (2, 2, _k(109, "KEY_PAGEDOWN", "PgDn")),
            (4, 1, _k(103, "KEY_UP", "\u2191")),
            (5, 0, _k(105, "KEY_LEFT", "\u2190")), (5, 1, _k(108, "KEY_DOWN", "\u2193")), (5, 2, _k(106, "KEY_RIGHT", "\u2192"))]
CODE_TO_NAME = {k[0]: k[1] for row in KEY_ROWS for k in row if k} | {k[0]: k[1] for _r, _c, k in NAV_KEYS}

# Qt key -> KEY_ name, for when the platform gives no scan code
_QT_KEYS = {Qt.Key_Escape: "KEY_ESC", Qt.Key_Tab: "KEY_TAB", Qt.Key_Backspace: "KEY_BACKSPACE",
            Qt.Key_Return: "KEY_ENTER", Qt.Key_Enter: "KEY_ENTER", Qt.Key_Space: "KEY_SPACE",
            Qt.Key_Shift: "KEY_LEFTSHIFT", Qt.Key_Control: "KEY_LEFTCTRL", Qt.Key_Alt: "KEY_LEFTALT",
            Qt.Key_Meta: "KEY_LEFTMETA", Qt.Key_CapsLock: "KEY_CAPSLOCK", Qt.Key_Insert: "KEY_INSERT",
            Qt.Key_Delete: "KEY_DELETE", Qt.Key_Home: "KEY_HOME", Qt.Key_End: "KEY_END",
            Qt.Key_PageUp: "KEY_PAGEUP", Qt.Key_PageDown: "KEY_PAGEDOWN", Qt.Key_Up: "KEY_UP",
            Qt.Key_Down: "KEY_DOWN", Qt.Key_Left: "KEY_LEFT", Qt.Key_Right: "KEY_RIGHT",
            Qt.Key_Minus: "KEY_MINUS", Qt.Key_Equal: "KEY_EQUAL", Qt.Key_BracketLeft: "KEY_LEFTBRACE",
            Qt.Key_BracketRight: "KEY_RIGHTBRACE", Qt.Key_Backslash: "KEY_BACKSLASH", Qt.Key_Semicolon: "KEY_SEMICOLON",
            Qt.Key_Apostrophe: "KEY_APOSTROPHE", Qt.Key_QuoteLeft: "KEY_GRAVE", Qt.Key_Comma: "KEY_COMMA",
            Qt.Key_Period: "KEY_DOT", Qt.Key_Slash: "KEY_SLASH", Qt.Key_Menu: "KEY_COMPOSE"}
for _i in range(12):
    _QT_KEYS[getattr(Qt, f"Key_F{_i + 1}")] = f"KEY_F{_i + 1}"
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _QT_KEYS[getattr(Qt, f"Key_{_c}")] = f"KEY_{_c}"
for _d in "0123456789":
    _QT_KEYS[getattr(Qt, f"Key_{_d}")] = f"KEY_{_d}"


def key_name_of(e) -> str:
    """The KEY_ name for a Qt key event: from the kernel scan code when the
    platform provides it (X11/Wayland: evdev code + 8 -- tells left and
    right Shift apart), else from the Qt key."""
    sc = e.nativeScanCode()
    if sc > 8 and (sc - 8) in CODE_TO_NAME:
        return CODE_TO_NAME[sc - 8]
    return _QT_KEYS.get(e.key()) or f"KEY_{e.key()}"


MOUSE_NAMES = {Qt.LeftButton: "BTN_LEFT", Qt.RightButton: "BTN_RIGHT", Qt.MiddleButton: "BTN_MIDDLE",
               Qt.BackButton: "BTN_SIDE", Qt.ForwardButton: "BTN_EXTRA"}


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
    """The capture surface: click (or focus and press keys) here. Draws a
    keyboard and a mouse that light up as you use them, counts events for
    the CPS numbers, and feeds every event to `log` (the macro-equivalent
    readout). Keeps focus so Space etc. land here instead of activating
    buttons.

    Per-event work stays tiny (counter bump, set update, tuple append);
    repainting is done by the page's timer via `dirty`."""

    def __init__(self, clicks: RateCounter, keys: RateCounter, held_changed, log: InputLog, parent=None):
        super().__init__(parent)
        self.clicks, self.keys = clicks, keys
        self.held_changed = held_changed
        self.log = log
        self.held_keys: set[str] = set()
        self.held_buttons: set[str] = set()
        self.held_names: set[str] = set()       # KEY_/BTN_ names, what the drawing lights up
        self.dirty = True
        self._last_pos = None
        self._last_move = (0, 0)
        self._wheel_flash = 0.0                 # perf_counter deadline
        self._wheel_dir = 0
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(330)
        self.setCursor(Qt.PointingHandCursor)
        self.hint = "Click here, then press keys / click / scroll / move -- the macro for it appears below"

    def _button_name(self, b) -> str:
        return {Qt.LeftButton: "LMB", Qt.RightButton: "RMB", Qt.MiddleButton: "MMB",
                Qt.BackButton: "MB4", Qt.ForwardButton: "MB5"}.get(b, "button")

    def wants_repaint(self, now: float) -> bool:
        return self.dirty or (self._wheel_dir != 0 and now > self._wheel_flash)

    def mousePressEvent(self, e) -> None:
        self.clicks.count += 1
        self.setFocus()
        self.held_buttons.add(self._button_name(e.button()))
        name = MOUSE_NAMES.get(e.button())
        if name:
            self.held_names.add(name)
            self.log.add(time.perf_counter(), "kd", name)
        self.dirty = True
        self.held_changed()

    mouseDoubleClickEvent = mousePressEvent

    def mouseReleaseEvent(self, e) -> None:
        self.held_buttons.discard(self._button_name(e.button()))
        name = MOUSE_NAMES.get(e.button())
        if name:
            self.held_names.discard(name)
            self.log.add(time.perf_counter(), "ku", name)
        self.dirty = True
        self.held_changed()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._last_pos is not None:
            dx, dy = round(pos.x() - self._last_pos.x()), round(pos.y() - self._last_pos.y())
            if dx or dy:
                self.log.add(time.perf_counter(), "move", dx, dy)
                self._last_move = (dx, dy)
                self.dirty = True
        self._last_pos = pos

    def leaveEvent(self, e) -> None:
        self._last_pos = None
        super().leaveEvent(e)

    def enterEvent(self, e) -> None:
        self._last_pos = e.position()
        super().enterEvent(e)

    def wheelEvent(self, e) -> None:
        n = e.angleDelta().y() / 120.0
        if n:
            n = int(n) if float(n).is_integer() else round(n, 2)
            self.log.add(time.perf_counter(), "wheel", n)
            self._wheel_dir = 1 if n > 0 else -1
            self._wheel_flash = time.perf_counter() + 0.25
            self.dirty = True
        e.accept()

    def keyPressEvent(self, e) -> None:
        if e.isAutoRepeat():
            return
        self.keys.count += 1
        self.held_keys.add(QKeySequence(e.key()).toString() or str(e.key()))
        name = key_name_of(e)
        self.held_names.add(name)
        self.log.add(time.perf_counter(), "kd", name)
        self.dirty = True
        self.held_changed()

    def keyReleaseEvent(self, e) -> None:
        if e.isAutoRepeat():
            return
        self.held_keys.discard(QKeySequence(e.key()).toString() or str(e.key()))
        name = key_name_of(e)
        self.held_names.discard(name)
        self.log.add(time.perf_counter(), "ku", name)
        self.dirty = True
        self.held_changed()

    def focusOutEvent(self, e) -> None:
        # the window lost the keys: release everything that was down so the
        # readout never shows a key stuck down forever
        now = time.perf_counter()
        for name in sorted(self.held_names):
            self.log.add(now, "ku", name)
        self.held_keys.clear()
        self.held_buttons.clear()
        self.held_names.clear()
        self.dirty = True
        self.held_changed()
        super().focusOutEvent(e)

    # -- drawing -----------------------------------------------------------
    def paintEvent(self, _e) -> None:
        self.dirty = False
        theme = Theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fill = theme.surface()
        if self.hasFocus():
            fill = fill.lighter(112)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.fillPath(rounded_rect_path(r, theme.corner_radius(12)), fill)
        text = contrast_text(fill)
        p.setPen(text)
        small = QFont(self.font())
        p.setFont(small)
        p.drawText(QRectF(r.left(), r.top() + 6, r.width(), 22), Qt.AlignCenter, self.hint)

        top = r.top() + 34
        avail_h = r.bottom() - top - 8
        # keyboard 18.5 units wide (main 15 + gap + nav 3) + mouse ~4.2 units
        u = max(10.0, min((r.width() - 24) / (18.5 + 0.8 + 4.2), avail_h / 6.3))
        gap = max(2.0, u * 0.08)
        x0 = r.left() + 12
        lit = theme.input_color()
        key_fill = fill.lighter(140) if fill.lightness() < 128 else fill.darker(108)
        edge = key_fill.darker(130)
        kf = QFont(self.font())
        kf.setPixelSize(max(8, int(u * 0.32)))
        p.setFont(kf)

        def draw_key(name, label, x, y, w):
            rect = QRectF(x, y, w * u - gap, u - gap)
            down = name in self.held_names
            p.setPen(QPen(edge, 1))
            p.setBrush(lit if down else key_fill)
            p.drawRoundedRect(rect, u * 0.14, u * 0.14)
            p.setPen(contrast_text(lit if down else key_fill))
            p.drawText(rect, Qt.AlignCenter, label)

        for ri, row in enumerate(KEY_ROWS):
            x = x0
            y = top + ri * u + (u * 0.35 if ri > 0 else 0)
            for k in row:
                if k is None:
                    x += u * 0.5
                    continue
                draw_key(k[1], k[2], x, y, k[3])
                x += k[3] * u
        nav_x = x0 + 15.5 * u
        for ri, col, k in NAV_KEYS:
            draw_key(k[1], k[2], nav_x + col * u, top + ri * u + u * 0.35, 1.0)

        # mouse
        mx = nav_x + 3.8 * u
        mw, mh = 2.6 * u, 4.0 * u
        my = top + 0.6 * u
        body = QRectF(mx, my, mw, mh)
        p.setPen(QPen(edge, 1))
        p.setBrush(key_fill)
        p.drawRoundedRect(body, mw * 0.42, mw * 0.42)
        bh = mh * 0.42

        def part(rect, name, label=""):
            down = name in self.held_names
            p.setPen(QPen(edge, 1))
            p.setBrush(lit if down else key_fill.lighter(112))
            p.drawRoundedRect(rect, u * 0.2, u * 0.2)
            if label:
                p.setPen(contrast_text(lit if down else key_fill))
                p.drawText(rect, Qt.AlignCenter, label)

        gw = mw * 0.06
        part(QRectF(mx + gw, my + gw, mw * 0.38, bh), "BTN_LEFT", "L")
        part(QRectF(mx + mw * 0.56, my + gw, mw * 0.38, bh), "BTN_RIGHT", "R")
        wheel = QRectF(mx + mw * 0.42, my + bh * 0.25, mw * 0.16, bh * 0.6)
        flash = self._wheel_dir != 0 and time.perf_counter() <= self._wheel_flash
        if not flash:
            self._wheel_dir = 0
        p.setPen(QPen(edge, 1))
        p.setBrush(lit if ("BTN_MIDDLE" in self.held_names or flash) else key_fill.lighter(112))
        p.drawRoundedRect(wheel, u * 0.1, u * 0.1)
        if flash:
            p.setPen(contrast_text(lit))
            p.drawText(wheel, Qt.AlignCenter, "\u25b2" if self._wheel_dir > 0 else "\u25bc")
        part(QRectF(mx - u * 0.18, my + mh * 0.42, u * 0.3, u * 0.6), "BTN_SIDE", "")
        part(QRectF(mx - u * 0.18, my + mh * 0.42 + u * 0.7, u * 0.3, u * 0.6), "BTN_EXTRA", "")
        p.setPen(text.darker(130))
        p.setFont(small)
        dx, dy = self._last_move
        p.drawText(QRectF(mx - u, my + mh + 6, mw + 2 * u, 20), Qt.AlignCenter, f"last move  {dx:+d}, {dy:+d}")


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

        self.log = InputLog()
        self.area = CpsArea(self.clicks, self.keys, self._update_held, self.log)
        self.content_layout.addWidget(self.area)

        row = QHBoxLayout()
        self.held = QLabel("Held: (nothing)")
        row.addWidget(self.held, stretch=1)
        reset = CustomButton("Reset")
        reset.clicked.connect(self.reset)
        row.addWidget(reset)
        self.content_layout.addLayout(row)

        self.macro_title = section_title("Macro equivalent")
        mark_output(self.macro_title)      # the code it shows is what a macro would *do*
        self.content_layout.addWidget(self.macro_title)
        self.content_layout.addWidget(dim_label(
            "What you just did, as a macro. Movement is measured inside the box above (as this window "
            "receives it, pointer acceleration included)."))
        opts = QHBoxLayout()
        self.combine = CustomCheckBox("Combine into tap / combo / wheel")
        self.combine.setChecked(True)
        self.combine.setToolTip("Off: exact key down / key up lines instead of tap() and combo().")
        self.waits = CustomCheckBox("Include waits")
        self.waits.setChecked(True)
        for cb in (self.combine, self.waits):
            cb.toggled.connect(self._log_dirty)
            opts.addWidget(cb)
        opts.addStretch(1)
        copy = CustomButton("Copy")
        copy.clicked.connect(self.copy_macro)
        clear = CustomButton("Clear")
        clear.clicked.connect(self.clear_macro)
        opts.addWidget(copy)
        opts.addWidget(clear)
        self.content_layout.addLayout(opts)
        self.macro_text = QPlainTextEdit()
        self.macro_text.setReadOnly(True)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.macro_text.setFont(f)
        self.macro_text.setMinimumHeight(170)
        self.macro_text.setPlaceholderText("Nothing yet -- use the keyboard / mouse above.")
        pal = self.macro_text.palette()
        pal.setColor(QPalette.Base, theme.surface())
        pal.setColor(QPalette.Text, theme.text())
        self.macro_text.setPalette(pal)
        self.content_layout.addWidget(self.macro_text)
        self._log_rev = -1
        self.content_layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(SAMPLE_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def reset(self) -> None:
        self.clicks.reset()
        self.keys.reset()
        self._tick()

    def _log_dirty(self, *_):
        self._log_rev = -1

    def clear_macro(self) -> None:
        self.log.clear()
        self.macro_text.clear()

    def copy_macro(self) -> None:
        QApplication.clipboard().setText(self.macro_text.toPlainText())

    def refresh_macro(self) -> None:
        """Rebuild the readout when new events arrived (called by the timer)."""
        if self._log_rev == self.log.revision:
            return
        self._log_rev = self.log.revision
        text = self.log.render(self.combine.isChecked(), self.waits.isChecked())
        if text != self.macro_text.toPlainText():
            self.macro_text.setPlainText(text)
            bar = self.macro_text.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _update_held(self) -> None:
        items = sorted(self.area.held_buttons) + sorted(self.area.held_keys)
        self.held.setText("Held: " + (" + ".join(items) if items else "(nothing)"))

    def _tick(self) -> None:
        now = time.perf_counter()
        if self.area.wants_repaint(now):
            self.area.update()
        self.refresh_macro()
        for name, c in (("Clicks", self.clicks), ("Key presses", self.keys)):
            c.sample(now)
            self.labels[(name, "current")].setText(f"{c.current:,.0f}")
            self.labels[(name, "peak")].setText(f"{c.peak:,.0f}")
            self.labels[(name, "avg")].setText(f"{c.average(now):,.0f}")
            self.labels[(name, "total")].setText(f"{c.count:,}")
