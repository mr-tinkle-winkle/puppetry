"""
Runs the C++ `puppetry-transcribe` helper and streams its generated code
into the editor. All timing work happens in that process (kernel
microsecond timestamps -- see native/src/transcriber.hpp); this side only
moves text.

Output is inserted in batches on a 50ms timer rather than per line:
precise mode produces ~2 lines per mouse frame (~2000 lines/s on a
1000Hz mouse), and one editor insert per line would bog the GUI down --
and was the likely source of the old "ghosted text" repaint glitch.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtGui import QGuiApplication

import puppetry_config as cfg


class TranscriptionController(QObject):
    text_ready = Signal(str)   # a batch of complete lines to insert
    started = Signal()
    stopped = Signal(str)      # status message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buf = ""
        self._focus_connected = False
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._flush)

    def running(self) -> bool:
        return self._proc is not None

    def start(self, *, keyboard_path, mouse_path, transcribe_keyboard, transcribe_mouse, raw,
              set_positions, same_start, raw_hz, precise, ping_key, abort_key=None, hotkey_key=None,
              ignore_alt_tab=False, ignore_puppetry=False) -> str | None:
        """Returns an error message, or None if started.

        `abort_key`, if given, makes the abort key end this session on its
        own (mirrors the daemon's own abort-key handling) -- see the exit
        code 3 check in _finished. `hotkey_key`, if given, is filtered out
        of the transcript like the ping key: the GUI's own toggle hotkey
        starts/stops this process, so its keypress should never itself be
        recorded, including the press that stops us. `ignore_alt_tab` drops
        Alt+Tab from the transcript; `ignore_puppetry` mutes everything
        while Puppetry's own window has focus."""
        if self._proc is not None:
            return None
        exe = cfg.find_binary("puppetry-transcribe")
        if not exe:
            return "Couldn't find the puppetry-transcribe helper (is Puppetry installed/built?)."
        args = ["--keyboard", keyboard_path or "", "--mouse", mouse_path or "",
                "--raw-hz", str(float(raw_hz)), "--ping-key", ping_key or "KEY_INSERT"]
        if abort_key:
            args += ["--abort-key", abort_key]
        if hotkey_key:
            args += ["--hotkey-key", hotkey_key]
        for flag, on in (("--transcribe-keyboard", transcribe_keyboard), ("--transcribe-mouse", transcribe_mouse),
                         ("--raw", raw), ("--set-positions", set_positions), ("--same-start", same_start),
                         ("--precise", precise), ("--ignore-alt-tab", ignore_alt_tab),
                         ("--ignore-puppetry", ignore_puppetry)):
            if on:
                args.append(flag)
        proc = QProcess(self)
        proc.setProgram(exe)
        proc.setArguments(args)
        proc.readyReadStandardOutput.connect(self._read)
        proc.finished.connect(self._finished)
        proc.start()
        if not proc.waitForStarted(3000):
            return f"Couldn't start the transcriber: {proc.errorString()}"
        self._proc = proc
        self._timer.start()
        if ignore_puppetry:
            self._watch_focus()
        self.started.emit()
        return None

    def stop(self) -> None:
        if self._proc is None:
            return
        self._unwatch_focus()
        self._proc.closeWriteChannel()  # EOF on its stdin = stop cleanly
        if not self._proc.waitForFinished(1000):
            self._proc.terminate()
            self._proc.waitForFinished(1000)

    # -- "Ignore Puppetry": we already know when our own window is focused
    # Qt tells us for free, so we tell the helper instead of making it ask
    # KWin (two kdotool subprocesses per poll -- see its header comment).
    def _watch_focus(self) -> None:
        app = QGuiApplication.instance()
        if app is None or self._focus_connected:
            return
        app.focusWindowChanged.connect(self._focus_changed)
        self._focus_connected = True
        # Whatever the state is right now, before any change happens.
        self._focus_changed(app.focusWindow())

    def _unwatch_focus(self) -> None:
        if not self._focus_connected:
            return
        app = QGuiApplication.instance()
        if app is not None:
            try:
                app.focusWindowChanged.disconnect(self._focus_changed)
            except (RuntimeError, TypeError):
                pass  # already gone (app shutting down)
        self._focus_connected = False

    def _focus_changed(self, window) -> None:
        # focusWindow() is None whenever no window of OURS has focus --
        # exactly the question the transcriber is asking.
        self.report_focus(window is not None)

    def report_focus(self, focused: bool) -> None:
        """Tells a running helper whether Puppetry's own window has focus.
        Harmless (and ignored) if it wasn't started with ignore_puppetry."""
        if self._proc is None:
            return
        try:
            self._proc.write(b"focus 1\n" if focused else b"focus 0\n")
        except RuntimeError:
            pass  # process died between the check and the write

    def _read(self) -> None:
        if self._proc is not None:
            self._buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")

    def _flush(self) -> None:
        # Only complete lines -- never insert half a line.
        cut = self._buf.rfind("\n")
        if cut >= 0:
            chunk, self._buf = self._buf[:cut + 1], self._buf[cut + 1:]
            self.text_ready.emit(chunk)

    def _finished(self, code: int, _status) -> None:
        self._unwatch_focus()
        self._read()
        if self._buf and not self._buf.endswith("\n"):
            self._buf += "\n"
        self._flush()
        self._timer.stop()
        err = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace").strip() if self._proc else ""
        self._proc = None
        if code == 2:
            self.stopped.emit("Couldn't open keyboard/mouse device -- check paths & permissions.")
        elif code == 3 or "abort_key_pressed" in err:
            self.stopped.emit("Stopped: abort key pressed.")
        elif "disconnected" in err:
            self.stopped.emit("Stopped: a device was disconnected.")
        elif code not in (0, 15) and err:
            self.stopped.emit(f"Stopped: {err}")
        else:
            self.stopped.emit("Stopped.")
