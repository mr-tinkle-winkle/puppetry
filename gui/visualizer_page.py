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

from PySide6.QtCore import QRectF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QWidget

import kbm_layout as kl
from input_transcript import InputLog
from kbm_paint import layout_for, needs_animation, paint_kbm, picture_size, theme_style
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.rounded_rect import rounded_rect_path
from ui_kit.theme import Theme, contrast_text
from widgets import PageBase, dim_label, label_style, section_title, mark_input, mark_output

WINDOW_S = 1.0     # "current" = events in the last second
SAMPLE_MS = 50


# Keyboard/mouse geometry + state live in kbm_layout (shared with the OBS
# pages, the Dictionary and the overlay renderer).
KEY_ROWS, NAV_KEYS, CODE_TO_NAME = kl.KEY_ROWS, kl.NAV_KEYS + kl.ARROW_KEYS, kl.CODE_TO_NAME

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
        self.kbm = kl.KbmState()                 # what the drawing shows (timers, arrow, wheel)
        self.style_ = theme_style(Theme())
        self.layout_ = layout_for(self.style_)
        self.stream_live = False                 # True: the picture follows the daemon's stream instead
        self.show_controller = False
        self.dirty = True
        self._last_pos = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(330)
        self.setCursor(Qt.PointingHandCursor)
        self.hint = "Click here, then press keys / click / scroll / move -- the macro for it appears below"

    def _button_name(self, b) -> str:
        return {Qt.LeftButton: "LMB", Qt.RightButton: "RMB", Qt.MiddleButton: "MMB",
                Qt.BackButton: "MB4", Qt.ForwardButton: "MB5"}.get(b, "button")

    # window events drive the picture only while the daemon's stream isn't there
    def _pic_key(self, name, down, now):
        if not self.stream_live:
            self.kbm.key(name, down, now)

    def _pic_move(self, dx, dy, now):
        if not self.stream_live:
            self.kbm.move(dx, dy, now)

    def _pic_wheel(self, n, now):
        if not self.stream_live:
            self.kbm.wheel(n, now)

    def set_show_controller(self, on: bool) -> None:
        self.show_controller = on
        base = layout_for(self.style_)
        self.layout_ = kl.combine_layouts(base, kl.build_controller_layout(), 1.2) if on else base
        self.dirty = True
        self.update()

    def set_stream_live(self, live: bool) -> None:
        if live != self.stream_live:
            self.stream_live = live
            self.kbm.clear()
            self.dirty = True

    def wants_repaint(self, now: float) -> bool:
        return self.dirty or needs_animation(self.kbm, self.style_, now)

    def mousePressEvent(self, e) -> None:
        self.clicks.count += 1
        self.setFocus()
        self.held_buttons.add(self._button_name(e.button()))
        name = MOUSE_NAMES.get(e.button())
        if name:
            now = time.perf_counter()
            self.held_names.add(name)
            self._pic_key(name, True, now)
            self.log.add(now, "kd", name)
        self.dirty = True
        self.held_changed()

    mouseDoubleClickEvent = mousePressEvent

    def mouseReleaseEvent(self, e) -> None:
        self.held_buttons.discard(self._button_name(e.button()))
        name = MOUSE_NAMES.get(e.button())
        if name:
            now = time.perf_counter()
            self.held_names.discard(name)
            self._pic_key(name, False, now)
            self.log.add(now, "ku", name)
        self.dirty = True
        self.held_changed()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._last_pos is not None:
            dx, dy = round(pos.x() - self._last_pos.x()), round(pos.y() - self._last_pos.y())
            if dx or dy:
                now = time.perf_counter()
                self.log.add(now, "move", dx, dy)
                self._pic_move(dx, dy, now)
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
            now = time.perf_counter()
            self.log.add(now, "wheel", n)
            self._pic_wheel(n, now)
            self.dirty = True
        e.accept()

    def keyPressEvent(self, e) -> None:
        if e.isAutoRepeat():
            return
        self.keys.count += 1
        self.held_keys.add(QKeySequence(e.key()).toString() or str(e.key()))
        name = key_name_of(e)
        now = time.perf_counter()
        self.held_names.add(name)
        self._pic_key(name, True, now)
        self.log.add(now, "kd", name)
        self.dirty = True
        self.held_changed()

    def keyReleaseEvent(self, e) -> None:
        if e.isAutoRepeat():
            return
        self.held_keys.discard(QKeySequence(e.key()).toString() or str(e.key()))
        name = key_name_of(e)
        now = time.perf_counter()
        self.held_names.discard(name)
        self._pic_key(name, False, now)
        self.log.add(now, "ku", name)
        self.dirty = True
        self.held_changed()

    def focusOutEvent(self, e) -> None:
        # the window lost the keys: release everything that was down so the
        # readout never shows a key stuck down forever
        now = time.perf_counter()
        for name in sorted(self.held_names):
            self.log.add(now, "ku", name)
            self._pic_key(name, False, now)
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
        p.setPen(contrast_text(fill))
        p.setFont(QFont(self.font()))
        p.drawText(QRectF(r.left(), r.top() + 6, r.width(), 22), Qt.AlignCenter, self.hint)
        top = r.top() + 34
        lay = self.layout_
        u = max(8.0, min((r.width() - 24) / lay["w"], (r.bottom() - top - 8) / lay["h"]))
        w, _h = picture_size(lay, self.style_, u)
        paint_kbm(p, lay, self.style_, self.kbm, time.perf_counter(), unit=u,
                  origin=(r.left() + (r.width() - w) / 2, top))


class StreamClient(QThread):
    """Reads the daemon's live event stream (every real input, and what
    macros send, marked) for the picture. Reconnects on its own; `live`
    says whether it's connected. Times are perf_counter at receipt."""

    event = Signal(str, str, str, float, float)   # src, type, name, a, b
    live = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        import socket as _socket
        import overlay_config as oc
        from overlay_server import load_code_names
        names = load_code_names()
        was = None
        while not self._stop:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            try:
                sock.connect(str(oc.event_socket()))
            except OSError:
                sock.close()
                if was is not False:
                    self.live.emit(False)
                    was = False
                for _ in range(20):
                    if self._stop:
                        return
                    self.msleep(100)
                continue
            sock.settimeout(0.3)
            self.live.emit(True)
            was = True
            buf = b""
            while not self._stop:
                try:
                    chunk = sock.recv(65536)
                except _socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    parts = ln.split()
                    try:
                        if parts[0] == b"h":
                            for c in parts[2:]:
                                self.event.emit("r", "k", names.get(int(c), f"CODE_{int(c)}"), 1, 0)
                        elif len(parts) == 5:
                            src, typ, a, b = parts[0].decode(), parts[2].decode(), int(parts[3]), int(parts[4])
                            if typ == "k":
                                self.event.emit(src, "k", names.get(a, f"CODE_{a}"), float(b), 0)
                            elif typ == "a":
                                ax = kl.AXIS_NAMES.get(a)
                                if ax:
                                    self.event.emit(src, "a", ax, b / 10000.0, 0)
                            else:
                                self.event.emit(src, typ, "", float(a), float(b))
                    except (ValueError, IndexError):
                        pass
            sock.close()
            self.live.emit(False)
            was = False


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
        self.source_lbl = dim_label("")
        self.show_pad = CustomCheckBox("Show controller")
        self.show_pad.setToolTip("Draw a game controller next to the mouse (needs the daemon: controller input "
                                 "comes from it).")
        self.show_pad.toggled.connect(self.area.set_show_controller)
        src_row = QHBoxLayout()
        src_row.addWidget(self.source_lbl, 1)
        src_row.addWidget(self.show_pad)
        self.content_layout.insertLayout(self.content_layout.indexOf(self.area) + 1, src_row)
        self.stream = StreamClient(self)
        self.stream.event.connect(self._stream_event)
        self.stream.live.connect(self._stream_live)
        self._stream_live(False)
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

        from overlay_settings import OverlaySection
        self.overlay = OverlaySection()
        self.content_layout.addWidget(self.overlay)
        self.content_layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(SAMPLE_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def hideEvent(self, e) -> None:
        self.overlay.flush()
        self.stream.stop()
        self.stream.wait(1000)
        super().hideEvent(e)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self.stream.isRunning():
            self.stream._stop = False
            self.stream.start()

    def _stream_live(self, live: bool) -> None:
        self.area.set_stream_live(live)
        self.source_lbl.setText(
            "Showing all input from the daemon -- yours in the input color, macros' in the output color."
            if live else "Showing this window's input only (the daemon isn't running). Colors, controller and "
                         "macro output need the daemon.")

    def _stream_event(self, src, typ, name, a, b) -> None:
        k = self.area.kbm
        now = time.perf_counter()
        if typ == "k":
            k.key(name, a == 1, now, src)
            if src == "r" and name in kl.PAD_BUTTONS and not self.show_pad.isChecked():
                self.show_pad.setChecked(True)          # a controller showed up: show it
        elif typ == "m":
            k.move(a, b, now, src)
        elif typ == "w":
            k.wheel(a, now, src)
        elif typ == "a":
            k.axis(name, a, now, src)
            if src == "r" and not self.show_pad.isChecked() and abs(a) > 0.5:
                self.show_pad.setChecked(True)
        self.area.dirty = True

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
