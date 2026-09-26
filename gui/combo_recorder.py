"""
Combo recorder for the macro editor's "Record Combo" button.

Reads the same keyboard/mouse devices the daemon watches (read-only,
same non-grabbing posture as the daemon itself) directly via python-evdev
-- the GUI needs its own raw view of physical input while recording,
independent of the daemon's own combo matching. Sends PAUSE/RESUME over
the control socket around the recording session (see
native/src/control_socket.cpp's PAUSE/RESUME commands and
macro_daemon.py's original HANDOFF note: "so an unrelated macro's combo
can't fire just because its keys happen to be held while a *different*
combo is recorded").

Auto-commits the combo once the currently-held key set has sat UNCHANGED
for `record_time` seconds (the Record Time spinner's value) -- not on
release. Held-set changes (a new key added, or a key released before the
timer fires) reset the timer, per the original spinner's documented
behavior ("how long a combo must sit still before the recorder
auto-commits it").

NOTE: this is a fresh implementation for the Qt port, reconstructed from
the documented control-socket PAUSE/RESUME behavior and the Record Time
spinner's documented semantics -- the original GTK recorder's exact
source wasn't available to port line-for-line this session. Behavior
should match, but this hasn't been exercised against a real GTK-vs-Qt
side-by-side comparison; flagged in the handoff.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import puppetry_config as cfg

try:
    from evdev import InputDevice, categorize, ecodes
    _HAVE_EVDEV = True
except ImportError:
    _HAVE_EVDEV = False


class ComboRecorder(QThread):
    finished_combo = Signal(list)  # list[str] of KEY_*/BTN_* names, empty if nothing recorded

    def __init__(self, keyboard_path: str, mouse_path: str, record_time: float, parent=None):
        super().__init__(parent)
        self.keyboard_path = keyboard_path
        self.mouse_path = mouse_path
        self.record_time = max(0.5, record_time)
        self._stop_requested = False
        # Compatibility alias -- some call sites connect to `.finished`,
        # which QThread already defines as "the thread ended" (no
        # payload). Route the actual result through finished_combo and
        # keep both usable.
        self.finished = self.finished_combo

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        if not _HAVE_EVDEV:
            self.finished_combo.emit([])
            return

        cfg.send_control_command({"cmd": "PAUSE"})
        try:
            result = self._record()
        finally:
            cfg.send_control_command({"cmd": "RESUME"})
        self.finished_combo.emit(result)

    def _record(self) -> list[str]:
        import selectors
        import time

        try:
            kb = InputDevice(self.keyboard_path)
            mouse = InputDevice(self.mouse_path)
        except OSError:
            return []

        sel = selectors.DefaultSelector()
        sel.register(kb.fd, selectors.EVENT_READ, kb)
        sel.register(mouse.fd, selectors.EVENT_READ, mouse)

        held: set[int] = set()
        last_change = time.monotonic()

        try:
            while not self._stop_requested:
                remaining = self.record_time - (time.monotonic() - last_change)
                if held and remaining <= 0:
                    break
                events = sel.select(timeout=max(0.05, remaining) if held else 0.5)
                for key, _mask in events:
                    dev = key.fileobj
                    for ev in dev.read():
                        if ev.type != ecodes.EV_KEY:
                            continue
                        if ev.value == 1:  # fresh down
                            if ev.code not in held:
                                held.add(ev.code)
                                last_change = time.monotonic()
                        elif ev.value == 0:  # up
                            if ev.code in held:
                                held.discard(ev.code)
                                last_change = time.monotonic()
        finally:
            kb.close()
            mouse.close()

        return sorted(_code_to_name(c) for c in held)


def _code_to_name(code: int) -> str:
    from evdev import ecodes
    name = ecodes.keys.get(code)
    if isinstance(name, list):
        name = name[0]
    return name or f"code:{code}"
