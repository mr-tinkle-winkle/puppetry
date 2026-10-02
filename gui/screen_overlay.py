"""
The on-screen overlay: the input visualizer drawn on the real screen, over
everything, click-through, in the same layout and look as the OBS pages.

    puppetry-overlay screen show | hide | toggle | stop | status
    puppetry-overlay screen run          (the overlay process itself)

One long-lived process (so toggling is instant), controlled through a Unix
socket at $XDG_RUNTIME_DIR/puppetry/screen-overlay.sock (one word per
line: show, hide, toggle, quit, status -> one JSON line back). `show` /
`toggle` start it when it isn't running -- through `systemd-run --user` when
possible, so it lives outside the daemon's service (a daemon restart, which
every macro save does, must not close it) and gets the graphical session's
environment.

On Wayland it runs through XWayland (QT_QPA_PLATFORM=xcb): an X11 window can
be override-redirect (never managed, stacked above everything including
fullscreen windows) and input-transparent, which a plain Wayland client
can't ask for. Without XWayland it falls back to a normal Wayland window
with the stays-on-top hint (KWin may or may not keep it on top).

It reads the daemon's event stream directly, so blocking the input
visualizer (ignored apps / the block toggle) applies here too.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import overlay_config as oc

UNIT = "puppetry-screen-overlay"


def socket_path() -> Path:
    return oc.runtime_dir() / "screen-overlay.sock"


# ---------------------------------------------------------------------------
# Client side (stdlib only)
# ---------------------------------------------------------------------------
def _ask(word: str, timeout: float = 2.0) -> dict | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path()))
        s.sendall((word + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode() or "{}")
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def _self_cmd() -> list:
    env = os.environ.get("PUPPETRY_OVERLAY_CMD")
    if env and os.access(env, os.X_OK):
        return [env]
    found = shutil.which("puppetry-overlay")
    if found:
        return [found]
    return [sys.executable, str(Path(__file__).resolve().parent / "overlay_cli.py")]


def session_env() -> dict:
    """This environment, plus the graphical session's display variables from the
    systemd user manager when they're missing (the daemon starts before the
    desktop, so a keybind's command doesn't have them)."""
    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True,
                             timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return env
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                 "QT_QPA_PLATFORMTHEME") and k not in env:
            env[k] = v
    return env


def _launch() -> str:
    cmd = _self_cmd() + ["screen", "run"]
    if shutil.which("systemd-run") and not os.environ.get("PUPPETRY_SCREEN_NO_SYSTEMD"):
        try:
            r = subprocess.run(["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet", "--", *cmd],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return "systemd-run"
        except (OSError, subprocess.TimeoutExpired):
            pass
    subprocess.Popen(cmd, env=session_env(), start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "process"


def control(action: str) -> dict:
    """show / hide / toggle / stop / status. Starts the overlay for show and toggle."""
    word = {"stop": "quit"}.get(action, action)
    got = _ask(word)
    if got is not None:
        return got
    if action in ("hide", "stop", "status"):
        return {"running": False, "visible": False}
    how = _launch()
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.15)
        got = _ask("show")
        if got is not None:
            got["started"] = how
            return got
    raise RuntimeError("the on-screen overlay didn't start (run `puppetry-overlay screen run` in a terminal "
                       "to see why)")


# ---------------------------------------------------------------------------
# The overlay process (Qt)
# ---------------------------------------------------------------------------
def placement(screen_rect, w: int, h: int, position: str, margin: int) -> tuple:
    """Top-left (x, y) of a w x h overlay at `position` inside screen_rect (x, y, w, h)."""
    sx, sy, sw, sh = screen_rect
    horiz = "center" if position in ("center", "top-center", "bottom-center") else \
        ("left" if position.endswith("left") else "right")
    vert = "center" if position == "center" else ("top" if position.startswith("top") else "bottom")
    x = sx + margin if horiz == "left" else (sx + sw - w - margin if horiz == "right" else sx + (sw - w) // 2)
    y = sy + margin if vert == "top" else (sy + sh - h - margin if vert == "bottom" else sy + (sh - h) // 2)
    return int(x), int(y)


def run(show: bool = False) -> int:
    env = session_env()
    for k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
        if k in env and k not in os.environ:
            os.environ[k] = env[k]
    if "QT_QPA_PLATFORM" not in os.environ:
        # XWayland when there is one: override-redirect + input-transparent is what an
        # always-on-top, click-through overlay needs, and plain Wayland can't ask for it
        os.environ["QT_QPA_PLATFORM"] = "xcb" if os.environ.get("DISPLAY") else "wayland"

    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QGuiApplication, QPainter
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication, QWidget

    import font_catalog
    import kbm_layout as kl
    from kbm_paint import needs_animation, paint_kbm
    from stream_client import StreamClient, apply_stream_event

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    font_catalog.register_qt_fonts()

    class Overlay(QWidget):
        def __init__(self):
            super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                             | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
                             | Qt.BypassWindowManagerHint)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.setWindowTitle("Puppetry on-screen overlay")
            self.kbm = kl.KbmState()
            self.blocked = False
            self.dirty = True
            self._mtime = self._cfg_mtime()
            self.reload()

        def reload(self) -> None:
            cfg = oc.load()
            self.cfg = cfg
            sc = dict(oc.DEFAULTS["screen"], **cfg.get("screen", {}))
            self.sc = sc
            self.scale = max(0.1, float(sc.get("scale", 60)) / 100.0)
            self.content = sc.get("content", "full")
            if self.content == "simple":
                self.style_ = dict(cfg["simple_style"])
                w, h = int(cfg["simple"].get("width", 900)), int(max(20, self.style_["font_px"] * 1.6))
            else:
                self.style_ = dict(cfg["style"])
                self.layout_ = kl.build_scene(cfg["scene"])
                w, h = kl.layout_pixel_size(self.layout_, self.style_)
            w, h = max(2, int(w * self.scale)), max(2, int(h * self.scale))
            scr = QGuiApplication.primaryScreen()
            mon = sc.get("monitor", "primary")
            screens = QGuiApplication.screens()
            if isinstance(mon, int) or (isinstance(mon, str) and mon.isdigit()):
                i = int(mon)
                if 0 <= i < len(screens):
                    scr = screens[i]
            g = scr.geometry() if scr else None
            rect = (g.x(), g.y(), g.width(), g.height()) if g else (0, 0, 1920, 1080)
            x, y = placement(rect, w, h, sc.get("position", "bottom-right"), int(sc.get("margin", 24)))
            self.setGeometry(x, y, w, h)
            self.dirty = True
            self.update()

        @staticmethod
        def _cfg_mtime():
            try:
                return oc.config_file().stat().st_mtime
            except OSError:
                return None

        def check_config(self) -> None:
            m = self._cfg_mtime()
            if m != self._mtime:
                self._mtime = m
                self.reload()

        def on_event(self, src, typ, name, a, b) -> None:
            apply_stream_event(self.kbm, src, typ, name, a, b, time.perf_counter())
            if typ == "p":
                self.blocked = bool(a)
            self.dirty = True

        def tick(self) -> None:
            if not self.isVisible():
                return
            now = time.perf_counter()
            if self.dirty or needs_animation(self.kbm, self.style_, now):
                self.dirty = False
                self.update()

        def paintEvent(self, _e) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setOpacity(max(0.05, min(1.0, float(self.sc.get("opacity", 100)) / 100.0)))
            p.scale(self.scale, self.scale)
            now = time.perf_counter()
            if self.content == "simple":
                from overlay_render import paint_simple
                paint_simple(p, self.kbm, self.style_, now, self.width() / self.scale, self.height() / self.scale)
            else:
                paint_kbm(p, self.layout_, self.style_, self.kbm, now)
            p.end()

    win = Overlay()
    stream = StreamClient()
    stream.event.connect(win.on_event)
    stream.live.connect(lambda live: (win.kbm.clear(), setattr(win, "dirty", True)) if not live else None)
    stream.start()

    frame = QTimer()
    frame.setInterval(16)
    frame.timeout.connect(win.tick)
    frame.start()
    cfg_timer = QTimer()
    cfg_timer.setInterval(1000)
    cfg_timer.timeout.connect(win.check_config)
    cfg_timer.start()

    server = QLocalServer()
    path = str(socket_path())
    socket_path().parent.mkdir(parents=True, exist_ok=True)
    QLocalServer.removeServer(path)
    if not server.listen(path):
        print(f"screen overlay: can't listen on {path}: {server.errorString()}", file=sys.stderr)
        return 1
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    def status() -> dict:
        return {"running": True, "visible": win.isVisible(), "blocked": win.blocked,
                "platform": QGuiApplication.platformName(), "geometry": [win.x(), win.y(), win.width(), win.height()]}

    def handle(conn) -> None:
        buf = bytes(conn.readAll()).decode(errors="replace")
        for word in buf.split():
            if word == "show":
                win.reload()
                win.show()
            elif word == "hide":
                win.hide()
            elif word == "toggle":
                if win.isVisible():
                    win.hide()
                else:
                    win.reload()
                    win.show()
            elif word == "quit":
                conn.write((json.dumps({"running": False, "visible": False}) + "\n").encode())
                conn.flush()
                conn.waitForBytesWritten(500)
                QTimer.singleShot(50, app.quit)
                return
        conn.write((json.dumps(status()) + "\n").encode())
        conn.flush()
        conn.disconnectFromServer()

    def on_conn() -> None:
        conn = server.nextPendingConnection()
        conn.readyRead.connect(lambda c=conn: handle(c))

    server.newConnection.connect(on_conn)
    if show:
        win.show()
    rc = app.exec()
    stream.stop()
    stream.wait(1000)
    server.close()
    QLocalServer.removeServer(path)
    return rc
