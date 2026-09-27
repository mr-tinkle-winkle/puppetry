"""
GUI-side input helpers, ported from macro_gui.py onto Qt threads:
combo recording, "press a key / move your mouse" device detection,
single-key detection (abort key, ping key), the live mouse-position
readout, and listing every input device.

These are human-speed interactions, so they stay in Python (python-evdev)
-- the timing-critical transcription is the C++ puppetry-transcribe
binary instead (see transcription.py). All results come back as Qt
signals, delivered on the GUI thread.
"""
from __future__ import annotations

import selectors
import subprocess
import time

from PySide6.QtCore import QThread, Signal

import puppetry_config as cfg

try:
    from evdev import InputDevice, ecodes as e, list_devices
    HAVE_EVDEV = True
except ImportError:  # the GUI still opens; recording/detection just report failure
    HAVE_EVDEV = False

OUR_VIRTUAL_DEVICE_NAMES = frozenset({"macro-daemon-virtual-keyboard", "macro-daemon-virtual-mouse"})


def key_name(code: int) -> str:
    if not HAVE_EVDEV:
        return str(code)
    name = e.keys.get(code, str(code))
    if isinstance(name, (list, tuple)):
        name = name[0]
    return name


def _names(code):
    names = e.keys.get(code, "")
    return (names,) if isinstance(names, str) else names


def is_key_name(code: int) -> bool:
    return HAVE_EVDEV and any(n.startswith("KEY_") for n in _names(code))


def is_button_name(code: int) -> bool:
    return HAVE_EVDEV and any(n.startswith("BTN_") for n in _names(code))


def resolve_key_code(name: str | None) -> int | None:
    """Name -> numeric code (the reverse of key_name), for turning a
    stored key name like the transcribe hotkey back into something we can
    compare evdev event codes against."""
    if not HAVE_EVDEV or not name:
        return None
    code = e.ecodes.get(name)
    return code if isinstance(code, int) else None


def _open(paths):
    sel = selectors.DefaultSelector()
    devices = []
    for path in paths:
        if not path:
            continue
        try:
            dev = InputDevice(path)
            devices.append(dev)
            sel.register(dev.fd, selectors.EVENT_READ, dev)
        except Exception:
            pass
    return sel, devices


def _close(devices):
    for dev in devices:
        try:
            dev.close()
        except Exception:
            pass


class ComboRecorder(QThread):
    """Waits for input, then auto-commits once whatever is held has stayed
    unchanged for `stable_seconds` (the Record Time setting). A brief
    click -- e.g. the one that pressed the record button -- releases long
    before that and is never captured. Pauses the daemon's own combo
    matching while recording (control-socket PAUSE/RESUME) so holding
    keys here can't fire some other macro."""

    progress = Signal(list, float)   # held names, seconds left (0 = captured)
    finished_combo = Signal(list, str)  # names, "ok" | "cancelled" | "no_devices"

    def __init__(self, keyboard_path, mouse_path, stable_seconds: float, parent=None):
        super().__init__(parent)
        self.paths = (keyboard_path, mouse_path)
        self.stable_seconds = max(0.5, float(stable_seconds))
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        if not HAVE_EVDEV:
            self.finished_combo.emit([], "no_devices")
            return
        cfg.send_control_command({"cmd": "PAUSE"})  # best-effort; fails fast if the daemon's down
        sel, devices = _open(self.paths)
        held: set[int] = set()
        last_change = time.monotonic()
        finalized = None
        try:
            if not devices:
                self.finished_combo.emit([], "no_devices")
                return
            while not self._cancel:
                changed = False
                for key, _ in sel.select(timeout=0.1):
                    try:
                        for ev in key.data.read():
                            if ev.type != e.EV_KEY:
                                continue
                            if ev.value == 1 and ev.code not in held:
                                held.add(ev.code)
                                changed = True
                            elif ev.value == 0 and ev.code in held:
                                held.discard(ev.code)
                                changed = True
                    except BlockingIOError:
                        pass
                now = time.monotonic()
                if changed:
                    last_change = now
                if held:
                    left = max(0.0, self.stable_seconds - (now - last_change))
                    self.progress.emit(sorted(key_name(c) for c in held), left)
                    if left <= 0:
                        finalized = sorted(key_name(c) for c in held)
                        break
                elif changed:
                    self.progress.emit([], self.stable_seconds)
        finally:
            _close(devices)
            cfg.send_control_command({"cmd": "RESUME"})  # unconditionally, however we got here
        if finalized is not None:
            self.finished_combo.emit(finalized, "ok")
        elif devices:
            self.finished_combo.emit([], "cancelled")


class DetectDevice(QThread):
    """kind "keyboard": first device to send a KEY_* press. kind "mouse":
    first to send relative motion or a BTN_* click. Never our own virtual
    devices. 10 second timeout."""

    found = Signal(object, object)  # path | None, name | None

    def __init__(self, kind: str, timeout: float = 10.0, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.timeout = timeout

    def run(self) -> None:
        if not HAVE_EVDEV:
            self.found.emit(None, None)
            return
        paths = []
        for path in list_devices():
            try:
                if InputDevice(path).name not in OUR_VIRTUAL_DEVICE_NAMES:
                    paths.append(path)
            except Exception:
                pass
        sel, devices = _open(paths)
        result = (None, None)
        try:
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline and result[0] is None:
                for key, _ in sel.select(timeout=0.2):
                    dev = key.data
                    try:
                        for ev in dev.read():
                            if self.kind == "keyboard" and ev.type == e.EV_KEY and ev.value == 1 and is_key_name(ev.code):
                                result = (dev.path, dev.name)
                            elif self.kind == "mouse" and (ev.type == e.EV_REL or (
                                    ev.type == e.EV_KEY and ev.value == 1 and is_button_name(ev.code))):
                                result = (dev.path, dev.name)
                            if result[0]:
                                break
                    except BlockingIOError:
                        pass
                    if result[0]:
                        break
        finally:
            _close(devices)
        self.found.emit(*result)


class DetectKey(QThread):
    """The next KEY_*/BTN_* press on the configured devices (abort key,
    ping key). 10 second timeout."""

    found = Signal(object, object)  # code | None, name | None

    def __init__(self, keyboard_path, mouse_path, timeout: float = 10.0, parent=None):
        super().__init__(parent)
        self.paths = (keyboard_path, mouse_path)
        self.timeout = timeout

    def run(self) -> None:
        if not HAVE_EVDEV:
            self.found.emit(None, None)
            return
        sel, devices = _open(self.paths)
        result = (None, None)
        try:
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline and result[0] is None:
                for key, _ in sel.select(timeout=0.2):
                    try:
                        for ev in key.data.read():
                            if ev.type == e.EV_KEY and ev.value == 1:
                                result = (ev.code, key_name(ev.code))
                                break
                    except BlockingIOError:
                        pass
        finally:
            _close(devices)
        self.found.emit(*result)


class HotkeyListener(QThread):
    """Like DetectKey, but doesn't stop after the first match and doesn't
    time out -- it watches for one specific code for as long as this
    thread runs, emitting `pressed` every time it goes down (value == 1).
    Used for the editor's "transcribe" toggle hotkey: the caller starts
    one of these while the editor page is open and stops it when leaving,
    so the hotkey only does anything there."""

    pressed = Signal()

    def __init__(self, keyboard_path, mouse_path, code: int, parent=None):
        super().__init__(parent)
        self.paths = (keyboard_path, mouse_path)
        self.code = code
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if not HAVE_EVDEV or self.code is None:
            return
        sel, devices = _open(self.paths)
        if not devices:
            return
        try:
            while not self._stop:
                for key, _ in sel.select(timeout=0.2):
                    try:
                        for ev in key.data.read():
                            if ev.type == e.EV_KEY and ev.value == 1 and ev.code == self.code:
                                self.pressed.emit()
                    except BlockingIOError:
                        pass
        finally:
            _close(devices)


def cursor_position(timeout: float = 0.5):
    """(x, y) via kdotool (KWin), exactly what move_mouse(move_to=True)
    uses at runtime; None if unavailable."""
    try:
        r = subprocess.run(["kdotool", "getmouselocation", "--shell"],
                           capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    x = y = None
    for line in r.stdout.splitlines():
        if line.startswith("X="):
            x = int(line[2:])
        elif line.startswith("Y="):
            y = int(line[2:])
    return (x, y) if x is not None and y is not None else None


class MousePositionPoller(QThread):
    """~4Hz cursor readout for the editor (each poll runs a small KWin
    script under the hood, so deliberately not faster)."""

    position = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        while not self._stop:
            pos = cursor_position()
            self.position.emit(f"{pos[0]}, {pos[1]}" if pos else "unavailable (is kdotool installed?)")
            for _ in range(5):
                if self._stop:
                    return
                time.sleep(0.05)


def list_all_devices() -> list[tuple[str, str]]:
    if not HAVE_EVDEV:
        return []
    out = []
    for path in list_devices():
        try:
            out.append((path, InputDevice(path).name))
        except Exception:
            pass
    return out
