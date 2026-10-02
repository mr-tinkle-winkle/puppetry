"""
The daemon's live event stream ($XDG_RUNTIME_DIR/puppetry/events.sock) for
Qt pictures: the in-app visualizer and the on-screen overlay.

StreamClient (a QThread) emits event(src, type, name, a, b) per line;
apply_stream_event() feeds one into a KbmState. Type "p" (src "s") is the
daemon blocking the input visualizer (an ignored app, or the user's toggle):
a = 1 blocked (release everything shown), 0 unblocked.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import kbm_layout as kl


def apply_stream_event(k: "kl.KbmState", src: str, typ: str, name: str, a: float, b: float, now: float) -> None:
    if typ == "k":
        k.key(name, a == 1, now, src)
    elif typ == "m":
        k.move(a, b, now, src)
    elif typ == "w":
        k.wheel(a, now, src)
    elif typ == "a":
        k.axis(name, a, now, src)
    elif typ == "p" and a:
        k.clear()


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
                            elif typ == "p":
                                self.event.emit(src, "p", "", float(a), 0)
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
