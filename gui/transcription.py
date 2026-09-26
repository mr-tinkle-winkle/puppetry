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

import puppetry_config as cfg


class TranscriptionController(QObject):
    text_ready = Signal(str)   # a batch of complete lines to insert
    started = Signal()
    stopped = Signal(str)      # status message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buf = ""
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._flush)

    def running(self) -> bool:
        return self._proc is not None

    def start(self, *, keyboard_path, mouse_path, transcribe_keyboard, transcribe_mouse, raw,
              set_positions, same_start, raw_hz, precise, ping_key) -> str | None:
        """Returns an error message, or None if started."""
        if self._proc is not None:
            return None
        exe = cfg.find_binary("puppetry-transcribe")
        if not exe:
            return "Couldn't find the puppetry-transcribe helper (is Puppetry installed/built?)."
        args = ["--keyboard", keyboard_path or "", "--mouse", mouse_path or "",
                "--raw-hz", str(float(raw_hz)), "--ping-key", ping_key or "KEY_INSERT"]
        for flag, on in (("--transcribe-keyboard", transcribe_keyboard), ("--transcribe-mouse", transcribe_mouse),
                         ("--raw", raw), ("--set-positions", set_positions), ("--same-start", same_start),
                         ("--precise", precise)):
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
        self.started.emit()
        return None

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.closeWriteChannel()  # EOF on its stdin = stop cleanly
        if not self._proc.waitForFinished(1000):
            self._proc.terminate()
            self._proc.waitForFinished(1000)

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
        self._read()
        if self._buf and not self._buf.endswith("\n"):
            self._buf += "\n"
        self._flush()
        self._timer.stop()
        err = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace").strip() if self._proc else ""
        self._proc = None
        if code == 2:
            self.stopped.emit("Couldn't open keyboard/mouse device -- check paths & permissions.")
        elif "disconnected" in err:
            self.stopped.emit("Stopped: a device was disconnected.")
        elif code not in (0, 15) and err:
            self.stopped.emit(f"Stopped: {err}")
        else:
            self.stopped.emit("Stopped.")
