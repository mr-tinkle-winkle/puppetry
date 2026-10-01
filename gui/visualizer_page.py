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

import copy
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
        self.style_["padding"] = 0
        self.scene = copy.deepcopy(kl.DEFAULT_SCENE)
        self.layout_ = kl.build_scene(self.scene, normalize=False)
        self.stream_live = False                 # True: the picture follows the daemon's stream instead
        self.show_controller = False
        # Edit mode: move / resize / add / remove the scene's elements
        self.editing = False
        self.selected: str | None = None
        self._drag = None                        # ("move"|"scale", el, start units, start x, y, scale)
        self._geom = (0.0, 0.0, 10.0)            # origin x, origin y (px of scene 0,0), px per unit
        self.on_scene_changed = None             # callback(scene)
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

    def set_show_controller(self, on: bool) -> None:      # (kept for old callers)
        if on and not kl.scene_has(self.scene, "controller"):
            self.add_element("controller")

    # -- the scene / Edit mode --------------------------------------------------
    def set_scene(self, scene: dict) -> None:
        self.scene = copy.deepcopy(scene)
        self.layout_ = kl.build_scene(self.scene, normalize=False)
        self.dirty = True
        self.update()

    def _scene_changed(self) -> None:
        self.layout_ = kl.build_scene(self.scene, normalize=False)
        self.dirty = True
        self.update()
        if self.on_scene_changed:
            self.on_scene_changed(copy.deepcopy(self.scene))

    def set_editing(self, on: bool) -> None:
        self.editing = on
        self.selected = None
        self._drag = None
        self.setCursor(Qt.ArrowCursor if on else Qt.PointingHandCursor)
        self.hint = ("Edit: drag to move, drag the corner square to resize, right-click for options"
                     if on else "Click here, then press keys / click / scroll / move -- the macro for it appears below")
        self.dirty = True
        self.update()

    def element(self, el_id):
        return next((e for e in self.scene["elements"] if e.get("id") == el_id), None)

    def add_element(self, typ: str) -> dict:
        right = max((kl.element_rect(e)[0] + kl.element_rect(e)[2] for e in self.scene["elements"]), default=-1.0)
        el = kl.default_element(self.scene, typ, right + 1.0, 0.0)
        self.scene["elements"].append(el)
        self.selected = el["id"]
        self._scene_changed()
        return el

    def remove_element(self, el_id) -> None:
        self.scene["elements"] = [e for e in self.scene["elements"] if e.get("id") != el_id]
        if self.selected == el_id:
            self.selected = None
        self._scene_changed()

    def reset_scene(self) -> None:
        self.scene = copy.deepcopy(kl.DEFAULT_SCENE)
        self.selected = None
        self._scene_changed()

    def _units(self, pos):
        ox, oy, u = self._geom
        return (pos.x() - ox) / u, (pos.y() - oy) / u

    def _hit(self, pos):
        """(element id, on its resize handle?) under `pos`, topmost first."""
        ox, oy, u = self._geom
        for el in reversed(self.scene["elements"]):
            x, y, w, h = kl.element_rect(el)
            hx, hy = ox + (x + w) * u, oy + (y + h) * u
            if el.get("id") == self.selected and abs(pos.x() - hx) <= 9 and abs(pos.y() - hy) <= 9:
                return el["id"], True
            if ox + x * u <= pos.x() <= ox + (x + w) * u and oy + y * u <= pos.y() <= oy + (y + h) * u:
                return el["id"], False
        return None, False

    def _edit_press(self, e) -> None:
        el_id, handle = self._hit(e.position())
        self.selected = el_id
        if el_id is not None and e.button() == Qt.LeftButton:
            el = self.element(el_id)
            ux, uy = self._units(e.position())
            self._drag = ("scale" if handle else "move", el_id, ux, uy, float(el.get("x", 0)),
                          float(el.get("y", 0)), float(el.get("scale", 1.0)))
        self.update()

    def _edit_move(self, e) -> None:
        if not self._drag:
            el_id, handle = self._hit(e.position())
            self.setCursor(Qt.SizeFDiagCursor if handle else (Qt.OpenHandCursor if el_id else Qt.ArrowCursor))
            return
        mode, el_id, sx, sy, x0, y0, s0 = self._drag
        el = self.element(el_id)
        if el is None:
            return
        ux, uy = self._units(e.position())
        if mode == "move":
            el["x"] = round((x0 + ux - sx) * 4) / 4          # quarter-key snapping
            el["y"] = round((y0 + uy - sy) * 4) / 4
        else:
            nat = kl.element_layout(el)
            want = max(0.3, min(4.0, (ux - x0) / max(nat["w"], 0.1)))
            el["scale"] = round(want * 20) / 20
        self.layout_ = kl.build_scene(self.scene, normalize=False)
        self.dirty = True
        self.update()

    def _edit_release(self, _e) -> None:
        if self._drag:
            self._drag = None
            self._scene_changed()

    def _edit_menu(self, e) -> None:
        from PySide6.QtWidgets import QMenu
        el_id, _h = self._hit(e.pos())
        menu = QMenu(self)
        if el_id is None:
            add = menu.addMenu("Add element")
            for typ, label in kl.ELEMENT_TYPES.items():
                add.addAction(label, lambda t=typ: self.add_element(t))
            menu.exec(e.globalPos())
            return
        self.selected = el_id
        el = self.element(el_id)
        typ = el.get("type")
        menu.addSection(f"{kl.ELEMENT_TYPES.get(typ, typ)} ({el_id})")

        def setter(k, v):
            def go():
                el[k] = v
                self._scene_changed()
            return go
        if typ == "keyboard":
            sub = menu.addMenu("Layout")
            for k, label in kl.KEYBOARD_PRESETS.items():
                a = sub.addAction(label, setter("layout", k))
                a.setCheckable(True)
                a.setChecked(el.get("layout", "tkl") == k)
        elif typ == "mouse":
            sub = menu.addMenu("Look")
            for k, label in kl.MOUSE_LOOKS.items():
                a = sub.addAction(label, setter("look", k))
                a.setCheckable(True)
                a.setChecked(el.get("look", "classic") == k)
        if typ == "comet":
            sub = menu.addMenu("Keep centered")
            for k, label in (("tail", "Tail centered"), ("head", "Head centered")):
                a = sub.addAction(label, setter("center", k))
                a.setCheckable(True)
                a.setChecked(el.get("center", "tail") == k)
        if typ in kl.MOTION_TYPES:
            sub = menu.addMenu("Show as")
            for k, label in kl.MOTION_TYPES.items():
                a = sub.addAction(label, setter("type", k))
                a.setCheckable(True)
                a.setChecked(typ == k)
        menu.addAction("Actual size", setter("scale", 1.0))
        menu.addSeparator()
        menu.addAction("Remove", lambda: self.remove_element(el_id))
        menu.exec(e.globalPos())

    def contextMenuEvent(self, e) -> None:
        if self.editing:
            self._edit_menu(e)

    def set_stream_live(self, live: bool) -> None:
        if live != self.stream_live:
            self.stream_live = live
            self.kbm.clear()
            self.dirty = True

    def wants_repaint(self, now: float) -> bool:
        return self.dirty or needs_animation(self.kbm, self.style_, now)

    def mousePressEvent(self, e) -> None:
        if self.editing:
            return self._edit_press(e)
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

    def mouseDoubleClickEvent(self, e) -> None:
        self.mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self.editing:
            return self._edit_release(e)
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
        if self.editing:
            return self._edit_move(e)
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
        if self.editing:
            return None
        n = e.angleDelta().y() / 120.0
        if n:
            n = int(n) if float(n).is_integer() else round(n, 2)
            now = time.perf_counter()
            self.log.add(now, "wheel", n)
            self._pic_wheel(n, now)
            self.dirty = True
        e.accept()

    def keyPressEvent(self, e) -> None:
        if self.editing:
            return None
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
        if self.editing:
            return None
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
        if self.hasFocus() and not self.editing:
            fill = fill.lighter(112)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.fillPath(rounded_rect_path(r, theme.corner_radius(12)), fill)
        p.setPen(contrast_text(fill))
        p.setFont(QFont(self.font()))
        p.drawText(QRectF(r.left(), r.top() + 6, r.width(), 22), Qt.AlignCenter, self.hint)
        top = r.top() + (56 if self.editing else 34)       # room for the element labels while editing
        lay = self.layout_
        rects = [kl.element_rect(e) for e in self.scene["elements"]]
        if rects:
            minx, miny = min(x for x, *_ in rects), min(y for _x, y, *_ in rects)
            maxx = max(x + w for x, _y, w, _h in rects)
            maxy = max(y + h for _x, y, _w, h in rects)
        else:
            minx, miny, maxx, maxy = 0.0, 0.0, 10.0, 4.0
        if self.editing:                                    # room to grow while editing
            maxx += 2
            maxy += 1
        sw, sh = max(maxx - minx, 1.0), max(maxy - miny, 1.0)
        u = max(6.0, min((r.width() - 24) / sw, (r.bottom() - top - 8) / sh))
        ox = r.left() + (r.width() - sw * u) / 2 - minx * u
        oy = top - miny * u
        self._geom = (ox, oy, u)
        now = time.perf_counter()
        paint_kbm(p, lay, self.style_, self.kbm, now, unit=u, origin=(ox, oy))
        if self.editing:
            from PySide6.QtGui import QPen
            acc = theme.button_color()
            f = QFont(self.font())
            f.setPointSizeF(max(7.0, f.pointSizeF() * 0.85))
            p.setFont(f)
            for el in self.scene["elements"]:
                x, y, w, h = kl.element_rect(el)
                box = QRectF(ox + x * u, oy + y * u, w * u, h * u)
                sel = el.get("id") == self.selected
                pen = QPen(acc if sel else contrast_text(fill), 2 if sel else 1, Qt.SolidLine if sel else Qt.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(box)
                p.drawText(QRectF(box.left(), box.top() - 18, max(box.width(), 220.0), 16), Qt.AlignLeft,
                           f"{kl.ELEMENT_TYPES.get(el.get('type'), el.get('type'))}  ({el.get('id')})")
                if sel:
                    p.setBrush(acc)
                    p.drawRect(QRectF(box.right() - 6, box.bottom() - 6, 12, 12))


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
        self.edit_btn = CustomButton("Edit layout")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setToolTip("Move, resize, add and remove what's shown -- keyboard (half, 60%, 80%, full), "
                                 "mouse, controller, and the Comet / Mousepad / Joystick movement views. "
                                 "The same arrangement is what OBS shows.")
        self.edit_btn.toggled.connect(self._toggle_edit)
        self.add_el_btn = CustomButton("+ Add element")
        self.add_el_btn.clicked.connect(self._add_menu)
        self.reset_layout_btn = CustomButton("Reset layout")
        self.reset_layout_btn.clicked.connect(self.area.reset_scene)
        for b in (self.add_el_btn, self.reset_layout_btn):
            b.setVisible(False)
        self.pad_hint = dim_label("")
        src_row = QHBoxLayout()
        src_row.addWidget(self.source_lbl, 1)
        src_row.addWidget(self.pad_hint)
        src_row.addWidget(self.add_el_btn)
        src_row.addWidget(self.reset_layout_btn)
        src_row.addWidget(self.edit_btn)
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
        self.area.set_scene(self.overlay.cfg["scene"])
        self.area.on_scene_changed = self.overlay.set_scene
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

    def _controller_seen(self) -> None:
        if not kl.scene_has(self.area.scene, "controller"):
            self.pad_hint.setText("Controller detected -- add it with Edit layout.")

    def _toggle_edit(self, on: bool) -> None:
        self.area.set_editing(on)
        self.edit_btn.setText("Done" if on else "Edit layout")
        for b in (self.add_el_btn, self.reset_layout_btn):
            b.setVisible(on)
        if not on:
            self.overlay.flush()
        if kl.scene_has(self.area.scene, "controller"):
            self.pad_hint.setText("")

    def _add_menu(self) -> None:
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        for typ, label in kl.ELEMENT_TYPES.items():
            menu.addAction(label, lambda t=typ: self.area.add_element(t))
        menu.exec(QCursor.pos())

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
            if src == "r" and name in kl.PAD_BUTTONS:
                self._controller_seen()
        elif typ == "m":
            k.move(a, b, now, src)
        elif typ == "w":
            k.wheel(a, now, src)
        elif typ == "a":
            k.axis(name, a, now, src)
            if src == "r" and abs(a) > 0.5:
                self._controller_seen()
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
