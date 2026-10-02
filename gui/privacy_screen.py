"""
"Privacy & on-screen overlay": the Input Visualizer page's section for

  * blocking the input visualizer -- the manual toggle and the ignored apps
    (the daemon's privacy.cpp; while blocked nothing reaches the OBS pages,
    the replay buffer afterglow reads, the in-app picture or the on-screen
    overlay);
  * the on-screen overlay (screen_overlay.py: click-through, over
    everything);
  * "viewers only": the OBS share window (overlay_cli.share);
  * a keybind for each of the three (overlay.json "hotkeys"; the daemon
    compiles them into built-in macros at start, so changing one restarts it).

It edits the same overlay.json dict as OverlaySection (section.cfg) and saves
through section.changed().
"""
from __future__ import annotations

import subprocess
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QMenu, QVBoxLayout,
                               QWidget)

import overlay_config as oc
import puppetry_config as cfg_mod
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.custom_spinbox import CustomSpinBox
from ui_kit.theme import Theme
from widgets import dim_label, label_style, section_title

MATCH_CHOICES = [("class", "App"), ("title", "Window title contains")]
WHEN_CHOICES = [("focused", "While focused"), ("open", "While open")]


def list_open_windows(limit: int = 40) -> list:
    """[(app class, title)] of the open windows, via kdotool (KWin); [] when unavailable."""
    def kd(*args):
        try:
            r = subprocess.run(["kdotool", *args], capture_output=True, text=True, timeout=2)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    out, seen = [], set()
    for wid in kd("search", "--class", ".").split()[:limit]:
        cls = kd("getwindowclassname", wid)
        title = kd("getwindowname", wid)
        if cls and cls not in seen and "puppetry" not in cls.lower():
            seen.add(cls)
            out.append((cls, title))
    return sorted(out, key=lambda t: t[0].lower())


def describe_block(st: dict) -> str:
    if not st:
        return "Status unknown (the daemon isn't running)."
    if st.get("manual"):
        return "BLOCKED (turned off by hand). Nothing is shown, sent to OBS or recorded."
    if st.get("blocked"):
        why = "is focused" if st.get("why") == "focused" else "is open"
        return f"BLOCKED: an ignored app ({st.get('app', '?')}) {why}. Nothing is shown, sent to OBS or recorded."
    note = "" if st.get("watching", True) or not st.get("rules") else \
        "  (Can't see windows: is kdotool installed? Ignored apps won't block until it is.)"
    return "Live." + note


class _Relay(QObject):
    done = Signal(str, object)      # what, result


class KeybindRow(QWidget):
    """Shows a keybind (list of key names) with Record and Clear; on_change(names)."""

    def __init__(self, names: list, on_change, parent=None):
        super().__init__(parent)
        self.names = list(names or [])
        self.on_change = on_change
        self._rec = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setStyleSheet(label_style(Theme().text()))
        self.record_btn = CustomButton("Record")
        self.record_btn.clicked.connect(self._record)
        self.clear_btn = CustomButton("Clear")
        self.clear_btn.clicked.connect(self._clear)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.record_btn)
        lay.addWidget(self.clear_btn)
        self._show()

    def _show(self, text: str | None = None) -> None:
        self.label.setText(text or (" + ".join(self.names) if self.names else "(no keybind)"))

    def _record(self) -> None:
        if self._rec is not None:
            self._rec.cancel()
            return
        from input_tools import ComboRecorder
        st = cfg_mod.load_state()
        kb, mouse = st.get("keyboard_path"), st.get("mouse_path")
        if not kb and not mouse:
            self._show("Set a keyboard/mouse in Settings first.")
            return
        self._rec = ComboRecorder(kb, mouse, float(st.get("record_time_seconds", 3.0)), self)
        self._rec.progress.connect(self._progress)
        self._rec.finished_combo.connect(self._done)
        self.record_btn.setText("Cancel")
        self._rec.start()

    def _progress(self, names: list, left: float) -> None:
        if not names:
            self._show("(listening... press and hold the keys)")
        elif left > 0:
            self._show(f"{' + '.join(names)}: hold steady ({left:.1f}s)")

    def _done(self, names: list, status: str) -> None:
        rec, self._rec = self._rec, None
        if rec is not None:
            rec.wait(500)
        self.record_btn.setText("Record")
        if status == "ok" and names:
            self.names = list(names)
            self.on_change(self.names)
        elif status == "no_devices":
            self._show("Couldn't open the keyboard/mouse (Settings > Devices).")
            return
        self._show()

    def _clear(self) -> None:
        if self._rec is None:
            self.names = []
            self.on_change([])
            self._show()


class PrivacyScreenSection(QWidget):
    def __init__(self, section, parent=None):
        super().__init__(parent)
        self.section = section
        self.cfg = section.cfg
        self.cfg.setdefault("ignored_apps", [])
        self.cfg.setdefault("hotkeys", {"screen": [], "block": [], "share": []})
        self.cfg.setdefault("screen", dict(oc.DEFAULTS["screen"]))
        self.relay = _Relay()
        self.relay.done.connect(self._async_done)
        self._combo_css = combo_box_stylesheet(theme_config.get_settings())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # -- blocking --------------------------------------------------------------
        lay.addWidget(section_title("Block the input visualizer"))
        lay.addWidget(dim_label(
            "While blocked, nothing you press, click or move is shown, sent to OBS or kept in the layered "
            "replay buffer (so afterglow never gets it), and macros' output isn't either."))
        row = QHBoxLayout()
        self.block_status = QLabel()
        self.block_status.setWordWrap(True)
        self.block_btn = CustomButton("Block now")
        self.block_btn.clicked.connect(self._toggle_block)
        row.addWidget(self.block_status, 1)
        row.addWidget(self.block_btn)
        lay.addLayout(row)
        self.block_key = self._keybind_row(lay, "block")

        lay.addWidget(section_title("Ignored apps"))
        lay.addWidget(dim_label(
            "Blocks automatically while one of these is focused, or while it has any window open. The name "
            "matches part of the app's id (e.g. keepassxc) or of a window title (e.g. Bitwarden)."))
        self.apps_box = QVBoxLayout()
        lay.addLayout(self.apps_box)
        add_row = QHBoxLayout()
        self.add_app_btn = CustomButton("+ Add app")
        self.add_app_btn.clicked.connect(self._add_app_menu)
        add_row.addWidget(self.add_app_btn)
        add_row.addStretch(1)
        lay.addLayout(add_row)
        self._rebuild_apps()

        # -- on-screen overlay -------------------------------------------------------
        lay.addWidget(section_title("On-screen overlay"))
        lay.addWidget(dim_label(
            "The input visualizer on your own screen: click-through, over every window (fullscreen games "
            "too), with the same layout and look as OBS."))
        row = QHBoxLayout()
        self.screen_btn = CustomButton("Show on screen")
        self.screen_btn.clicked.connect(lambda: self._screen("toggle"))
        self.screen_status = dim_label("")
        row.addWidget(self.screen_btn)
        row.addWidget(self.screen_status, 1)
        lay.addLayout(row)
        form = QFormLayout()
        sc = self.cfg["screen"]
        self.content = self._combo([("full", "Full layout"), ("simple", "Simple input list")],
                                   sc.get("content", "full"), lambda v: self._screen_set("content", v))
        self.position = self._combo(oc.SCREEN_POSITIONS, sc.get("position", "bottom-right"),
                                    lambda v: self._screen_set("position", v))
        from PySide6.QtGui import QGuiApplication
        mons = [("primary", "Primary")] + [(str(i), f"{i + 1}: {s.name()}")
                                           for i, s in enumerate(QGuiApplication.screens())]
        self.monitor = self._combo(mons, str(sc.get("monitor", "primary")), lambda v: self._screen_set("monitor", v))
        self.size_spin = self._spin(10, 300, sc.get("scale", 60), "%", lambda v: self._screen_set("scale", v))
        self.opacity = self._spin(5, 100, sc.get("opacity", 100), "%", lambda v: self._screen_set("opacity", v))
        self.margin = self._spin(0, 2000, sc.get("margin", 24), " px", lambda v: self._screen_set("margin", v))
        for label, w in (("Show", self.content), ("Where", self.position), ("Monitor", self.monitor),
                         ("Size", self.size_spin), ("Opacity", self.opacity), ("Distance from the edge", self.margin)):
            w.setMaximumWidth(380)
            form.addRow(self._lbl(label), w)
        lay.addLayout(form)
        self.screen_key = self._keybind_row(lay, "screen")

        # -- viewers only --------------------------------------------------------------
        lay.addWidget(section_title("Show it to viewers only"))
        lay.addWidget(dim_label(
            "A screen share or recording of your monitor captures exactly what your monitor shows, so an "
            "overlay that viewers see but you don't can't be drawn on the monitor itself. Two ways to get "
            "it anyway: in OBS, the overlay is already a source only OBS shows (Expose Input Visualizer to "
            "OBS). For Discord or a call, Open share window opens OBS's program output (your screen plus "
            "the overlay) in its own window: share that window instead of your screen, and keep it on "
            "another virtual desktop. OBS needs a screen capture source and its WebSocket server on."))
        row = QHBoxLayout()
        self.share_btn = CustomButton("Open share window")
        self.share_btn.clicked.connect(self._share)
        self.share_status = dim_label("")
        row.addWidget(self.share_btn)
        row.addWidget(self.share_status, 1)
        lay.addLayout(row)
        self.share_key = self._keybind_row(lay, "share")

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    # -- helpers ------------------------------------------------------------------
    def _lbl(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet(label_style(Theme().text()))
        return lb

    def _combo(self, choices, current, on_pick) -> QComboBox:
        c = QComboBox()
        c.setStyleSheet(self._combo_css)
        for v, lab in choices:
            c.addItem(lab, v)
        i = c.findData(current)
        c.setCurrentIndex(max(0, i))
        c.currentIndexChanged.connect(lambda _i: on_pick(c.currentData()))
        return c

    def _spin(self, lo, hi, val, suffix, on_change) -> CustomSpinBox:
        s = CustomSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(val))
        s.setSuffix(suffix)
        s.valueChanged.connect(lambda v: on_change(int(v)))
        return s

    def _keybind_row(self, lay, key: str) -> KeybindRow:
        row = QHBoxLayout()
        row.addWidget(self._lbl("Keybind:"))
        kb = KeybindRow(self.cfg["hotkeys"].get(key, []), lambda names, k=key: self._set_hotkey(k, names))
        row.addWidget(kb, 1)
        lay.addLayout(row)
        return kb

    def _set_hotkey(self, key: str, names: list) -> None:
        self.cfg["hotkeys"][key] = list(names)
        self.section.changed(restart=True)          # keybinds are compiled when the daemon starts

    # -- blocking --------------------------------------------------------------------
    def refresh(self) -> None:
        st = oc.read_privacy()
        self.block_status.setText(describe_block(st))
        self.block_btn.setText("Unblock" if st.get("manual") else "Block now")

    def _toggle_block(self) -> None:
        from overlay_cli import daemon_request
        resp = daemon_request({"cmd": "VISUALIZER", "state": "toggle"})
        if not resp.get("ok"):
            self.block_status.setText(resp.get("error", "The daemon didn't answer."))
            return
        QTimer.singleShot(150, self.refresh)

    # -- ignored apps ------------------------------------------------------------------
    def _rebuild_apps(self) -> None:
        while self.apps_box.count():
            it = self.apps_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.app_rows = []
        if not self.cfg["ignored_apps"]:
            self.apps_box.addWidget(dim_label("(none)"))
            return
        for i, rule in enumerate(self.cfg["ignored_apps"]):
            host = QWidget()
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            name = CustomLineEdit(rule.get("value", ""))
            name.editingFinished.connect(lambda i=i, w=name: self._app_set(i, "value", w.text().strip()))
            match = self._combo(MATCH_CHOICES, rule.get("match", "class"), lambda v, i=i: self._app_set(i, "match", v))
            when = self._combo(WHEN_CHOICES, rule.get("when", "focused"), lambda v, i=i: self._app_set(i, "when", v))
            rm = CustomButton("Remove")
            rm.clicked.connect(lambda _c=False, i=i: self._remove_app(i))
            row.addWidget(name, 2)
            row.addWidget(match, 1)
            row.addWidget(when, 1)
            row.addWidget(rm)
            self.apps_box.addWidget(host)
            self.app_rows.append({"name": name, "match": match, "when": when, "remove": rm})

    def _app_set(self, i: int, key: str, value) -> None:
        if 0 <= i < len(self.cfg["ignored_apps"]):
            self.cfg["ignored_apps"][i][key] = value
            self.section.changed(restart=False)      # the daemon re-reads overlay.json by itself

    def add_app(self, value: str, match: str = "class", when: str = "focused") -> None:
        value = (value or "").strip()
        if not value:
            return
        self.cfg["ignored_apps"].append({"match": match, "value": value, "when": when})
        self._rebuild_apps()
        self.section.changed(restart=False)

    def _remove_app(self, i: int) -> None:
        if 0 <= i < len(self.cfg["ignored_apps"]):
            del self.cfg["ignored_apps"][i]
            self._rebuild_apps()
            self.section.changed(restart=False)

    def _add_app_menu(self) -> None:
        self.add_app_btn.setEnabled(False)
        self.add_app_btn.setText("Looking at your windows...")
        threading.Thread(target=lambda: self.relay.done.emit("windows", list_open_windows()), daemon=True).start()

    def _show_app_menu(self, windows: list) -> None:
        self.add_app_btn.setEnabled(True)
        self.add_app_btn.setText("+ Add app")
        menu = QMenu(self)
        if windows:
            menu.addSection("Open apps")
            for cls, title in windows:
                short = title if len(title) <= 50 else title[:47] + "..."
                menu.addAction(f"{cls}    ({short})" if short else cls, lambda c=cls: self.add_app(c))
        else:
            act = menu.addAction("(couldn't list windows: is kdotool installed?)")
            act.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Type an app name...", self._type_app)
        menu.addAction("Type part of a window title...", lambda: self._type_app(title=True))
        menu.exec(QCursor.pos())

    def _type_app(self, title: bool = False) -> None:
        text, ok = QInputDialog.getText(self, "Ignored app",
                                        "Part of the window title:" if title else "App name (part of its id):")
        if ok:
            self.add_app(text, "title" if title else "class")

    # -- on-screen overlay -----------------------------------------------------------------
    def _screen_set(self, key: str, value) -> None:
        self.cfg["screen"][key] = value
        self.section.changed(restart=False)         # the overlay re-reads overlay.json by itself

    def _screen(self, action: str) -> None:
        self.section.flush()                         # so it starts with the current settings
        self.screen_btn.setEnabled(False)
        self.screen_status.setText("Starting..." if action != "hide" else "")

        def work():
            try:
                import screen_overlay
                self.relay.done.emit("screen", screen_overlay.control(action))
            except Exception as e:  # noqa: BLE001
                self.relay.done.emit("screen", {"error": str(e)})
        threading.Thread(target=work, daemon=True).start()

    # -- share -----------------------------------------------------------------------------
    def _share(self) -> None:
        self.section.flush()
        self.share_btn.setEnabled(False)
        self.share_status.setText("Asking OBS...")

        def work():
            try:
                import overlay_cli
                self.relay.done.emit("share", overlay_cli.share())
            except Exception as e:  # noqa: BLE001
                self.relay.done.emit("share", {"error": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def _async_done(self, what: str, res) -> None:
        if what == "windows":
            self._show_app_menu(res)
        elif what == "screen":
            self.screen_btn.setEnabled(True)
            if res.get("error"):
                self.screen_status.setText(res["error"])
            else:
                vis = res.get("visible")
                self.screen_btn.setText("Hide from screen" if vis else "Show on screen")
                self.screen_status.setText("On screen." if vis else "Hidden.")
        elif what == "share":
            self.share_btn.setEnabled(True)
            self.share_status.setText(res.get("error") or "Opened OBS's share window: share that window.")
