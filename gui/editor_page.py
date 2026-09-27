"""
Macro editor page. Left: settings column (name, description, repeat,
combo + trigger edge, mouse-position readout, transcription, ignore
toggles, references, custom button names). Right: the code. Bottom
right: Save (stays open) / Save and Close / Close.

Save validates with the daemon's own compilers (`puppetry-daemon
--check`), then commits into the model and applies it (writes config +
restarts the daemon), so a macro can be edited and tried repeatedly
without leaving the editor.
"""
from __future__ import annotations

import copy
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeyEvent, QPalette, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

import puppetry_config as cfg
from input_tools import ComboRecorder, DetectKey, HotkeyListener, MousePositionPoller, resolve_key_code
from model import AppModel
from reference import DICTIONARY_TEXT, alias_targets, simplified_names_reference_text
from transcription import TranscriptionController
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.custom_spinbox import CustomDoubleSpinBox
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme
from widgets import Collapsible, ask, dim_label, label_style, section_title

REPEAT_MODES = ["none", "hold", "toggle"]
REPEAT_LABELS = ["No Repeat", "Hold", "Toggle"]
EDGES = ["down", "up"]
EDGE_LABELS = ["On Press", "On Release"]


def blank_macro() -> dict:
    return {
        "id": None, "name": "New Macro", "description": "", "repeat_mode": "none",
        "combo": [], "trigger_edge": "down", "locked": False, "code": "",
        "simplified_names": True, "python_on": True,
        "ignore_keyboard": False, "ignore_mouse_buttons": False, "ignore_mouse_movement": False,
    }


class ZoomablePlainTextEdit(QPlainTextEdit):
    """Ctrl+Scroll zooms the code only; Ctrl+0 resets."""

    BASE_PT = 11

    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(self.BASE_PT)
        self.setFont(f)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            f = self.font()
            step = 1 if event.angleDelta().y() > 0 else -1
            f.setPointSize(max(6, min(40, f.pointSize() + step)))
            self.setFont(f)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_0:
            f = self.font()
            f.setPointSize(self.BASE_PT)
            self.setFont(f)
            return
        super().keyPressEvent(event)


class MacroEditorPage(QWidget):
    def __init__(self, model: AppModel, on_close, parent=None):
        super().__init__(parent)
        self.model = model
        self.on_close = on_close
        self.macro: dict = blank_macro()
        self._snapshot = ""
        self._recorder: ComboRecorder | None = None
        self._ping_detect: DetectKey | None = None
        self._hotkey_detect: DetectKey | None = None
        self._poller: MousePositionPoller | None = None
        self._hotkey_listener: HotkeyListener | None = None
        self.transcriber = TranscriptionController(self)
        theme = Theme()
        self._theme = theme
        css = combo_box_stylesheet(theme_config.get_settings())
        pad = theme.padding

        pal = self.palette()
        pal.setColor(QPalette.Window, theme.page_background())
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(pad, pad, pad, pad)
        root.setSpacing(pad)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, stretch=1)

        # ------------------------------------------------------------ left column
        left_scroll = SmoothScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # content wraps to the pane instead
        left = QWidget()
        left.setAutoFillBackground(False)
        col = QVBoxLayout(left)
        col.setSpacing(pad // 2 + 4)
        left_scroll.setWidget(left)
        left_scroll.setAutoFillBackground(False)
        left_scroll.viewport().setAutoFillBackground(False)
        left.setAutoFillBackground(False)
        splitter.addWidget(left_scroll)

        col.addWidget(QLabel("Name"))
        self.name_edit = CustomLineEdit()
        col.addWidget(self.name_edit)
        col.addWidget(QLabel("Description"))
        self.desc_edit = CustomLineEdit()
        col.addWidget(self.desc_edit)

        row = QHBoxLayout()
        row.addWidget(QLabel("Repeat mode"))
        self.repeat = QComboBox()
        self.repeat.addItems(REPEAT_LABELS)
        self.repeat.setStyleSheet(css)
        row.addWidget(self.repeat)
        row.addStretch(1)
        col.addLayout(row)

        col.addWidget(QLabel("Key combo"))
        combo_row = QHBoxLayout()
        self.combo_label = QLabel()
        self.combo_label.setWordWrap(True)
        self.combo_label.setMinimumWidth(10)
        combo_row.addWidget(self.combo_label, stretch=1)
        self.edge = QComboBox()
        self.edge.addItems(EDGE_LABELS)
        self.edge.setStyleSheet(css)
        combo_row.addWidget(self.edge)
        self.record_btn = CustomButton("Record Combo")
        self.record_btn.clicked.connect(self._record_clicked)
        combo_row.addWidget(self.record_btn)
        self.clear_combo_btn = CustomButton("✕")
        self.clear_combo_btn.setToolTip("Remove the combo")
        self.clear_combo_btn.clicked.connect(self._clear_combo)
        combo_row.addWidget(self.clear_combo_btn)
        col.addLayout(combo_row)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Mouse position"))
        self.mouse_pos = QLabel("—")
        self.mouse_pos.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mouse_pos.setFont(QFont("monospace"))
        pos_row.addWidget(self.mouse_pos, stretch=1)
        col.addLayout(pos_row)

        # ------------------------------------------------------------ transcription
        col.addWidget(section_title("Transcribe Inputs"))
        st = model.state
        self.tr_kb = CustomCheckBox("Transcribe Keyboard")
        self.tr_mouse = CustomCheckBox("Transcribe Mouse")
        self.tr_raw = CustomCheckBox("Raw Mouse Input")
        self.tr_setpos = CustomCheckBox("Set Mouse Positions")
        self.tr_samestart = CustomCheckBox("Same Starting Mouse Position")
        self.tr_precise = CustomCheckBox("Precise timing")
        self.tr_precise.setToolTip(
            "Every line's timing already comes from the kernel's own microsecond timestamps.\n"
            "This additionally records each hardware mouse frame at your mouse's real polling rate\n"
            "(instead of merging them to the sample rate below) and writes precise=True waits.\n"
            "Replaying the exact original frames also reproduces pointer acceleration faithfully.\n"
            "Cost: ~2 lines per mouse frame, and playback keeps a CPU core busy.")
        for cb in (self.tr_kb, self.tr_mouse, self.tr_raw, self.tr_setpos, self.tr_samestart, self.tr_precise):
            col.addWidget(cb)
        col.addWidget(dim_label("Timing always comes from the kernel's own microsecond timestamps. Precise timing "
                                "also records every mouse frame at your mouse's real polling rate and writes "
                                "precise waits -- the most faithful replay (pointer acceleration included), but "
                                "about 2 lines per mouse frame and a busy CPU core during playback."))
        hz_row = QHBoxLayout()
        hz_row.addWidget(QLabel("Raw sample rate (Hz)"))
        self.tr_hz = CustomDoubleSpinBox()
        self.tr_hz.setRange(1, 8000)
        self.tr_hz.setDecimals(0)
        self.tr_hz.setSingleStep(5)
        hz_row.addWidget(self.tr_hz)
        hz_row.addStretch(1)
        col.addLayout(hz_row)
        ping_row = QHBoxLayout()
        ping_row.addWidget(QLabel("Ping key"))
        self.ping_label = QLabel()
        self.ping_label.setFont(QFont("monospace"))
        ping_row.addWidget(self.ping_label, stretch=1)
        self.ping_btn = CustomButton("Change")
        self.ping_btn.clicked.connect(self._change_ping)
        ping_row.addWidget(self.ping_btn)
        col.addLayout(ping_row)
        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(QLabel("Transcribe hotkey"))
        self.hotkey_label = QLabel()
        self.hotkey_label.setFont(QFont("monospace"))
        hotkey_row.addWidget(self.hotkey_label, stretch=1)
        self.hotkey_btn = CustomButton("Change")
        self.hotkey_btn.clicked.connect(self._change_hotkey)
        hotkey_row.addWidget(self.hotkey_btn)
        col.addLayout(hotkey_row)
        col.addWidget(dim_label("While this page is open, pressing this key starts/stops transcribing (with "
                                "whatever's checked above) -- never itself recorded. The abort key always stops "
                                "transcribing too, from anywhere."))
        tr_row = QHBoxLayout()
        self.tr_btn = CustomButton("Start Transcribing")
        self.tr_btn.clicked.connect(self._transcribe_clicked)
        tr_row.addWidget(self.tr_btn)
        self.tr_status = QLabel("")
        self.tr_status.setWordWrap(True)
        self.tr_status.setMinimumWidth(10)
        tr_row.addWidget(self.tr_status, stretch=1)
        col.addLayout(tr_row)

        self.tr_kb.setChecked(bool(st.get("transcribe_keyboard", False)))
        self.tr_mouse.setChecked(bool(st.get("transcribe_mouse", False)))
        self.tr_raw.setChecked(bool(st.get("transcribe_raw", False)))
        self.tr_setpos.setChecked(bool(st.get("transcribe_setpos", False)))
        self.tr_samestart.setChecked(bool(st.get("transcribe_samestart", False)) and not self.tr_setpos.isChecked())
        self.tr_precise.setChecked(bool(st.get("transcribe_precise", False)))
        self.tr_hz.setValue(float(st.get("transcribe_raw_hz", 60)))
        self.ping_label.setText(st.get("transcribe_ping_key") or "KEY_INSERT")
        self.hotkey_label.setText(st.get("transcribe_hotkey") or "(not set)")
        self._sync_transcribe_enabled()
        for cb, key in ((self.tr_kb, "transcribe_keyboard"), (self.tr_mouse, "transcribe_mouse"),
                        (self.tr_raw, "transcribe_raw"), (self.tr_setpos, "transcribe_setpos"),
                        (self.tr_samestart, "transcribe_samestart"), (self.tr_precise, "transcribe_precise")):
            cb.toggled.connect(lambda on, key=key: self._transcribe_pref(key, on))
        self.tr_hz.valueChanged.connect(lambda v: model.set_pref("transcribe_raw_hz", v))
        self.tr_setpos.toggled.connect(lambda on: on and self.tr_samestart.setChecked(False))
        self.tr_samestart.toggled.connect(lambda on: on and self.tr_setpos.setChecked(False))
        self.transcriber.text_ready.connect(self._insert_transcribed)
        self.transcriber.stopped.connect(self._transcribe_stopped)

        # ------------------------------------------------------------ ignore
        col.addWidget(section_title("While this macro runs"))
        self.ign_kb = CustomCheckBox("Ignore Keyboard Input (except Abort)")
        self.ign_btn = CustomCheckBox("Ignore Mouse Buttons")
        self.ign_move = CustomCheckBox("Ignore Mouse Movement")
        for cb in (self.ign_kb, self.ign_btn, self.ign_move):
            col.addWidget(cb)
        col.addWidget(dim_label("Actually grabs the real device while this macro runs, so your own input can't "
                                "interfere. The abort key always force-releases these if something gets stuck."))

        # ------------------------------------------------------------ references + aliases
        ref = QLabel(DICTIONARY_TEXT)
        ref.setWordWrap(True)
        ref.setFont(QFont("monospace"))
        ref.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(Collapsible("Function reference", ref))
        simple = QLabel(simplified_names_reference_text())
        simple.setWordWrap(True)
        simple.setFont(QFont("monospace"))
        simple.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(Collapsible("Simplified name reference", simple))

        col.addWidget(section_title("Custom button names"))
        col.addWidget(dim_label("App-wide extra names on top of the simplified names (saved with this macro)."))
        self.alias_box = QVBoxLayout()
        col.addLayout(self.alias_box)
        add_alias = CustomButton("+ Add custom name")
        add_alias.clicked.connect(lambda: self._add_alias_row())
        add_row = QHBoxLayout()
        add_row.addWidget(add_alias)
        add_row.addStretch(1)
        col.addLayout(add_row)
        self._alias_rows: list[tuple[CustomLineEdit, QComboBox, QWidget]] = []
        col.addStretch(1)

        # ------------------------------------------------------------ right: code
        right = QWidget()
        rcol = QVBoxLayout(right)
        rcol.setContentsMargins(pad // 2, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(section_title("Macro code"))
        head.addStretch(1)
        self.simplified_cb = CustomCheckBox("Simplified Variable Names")
        self.python_cb = CustomCheckBox("Run as embedded Python")
        self.python_cb.setToolTip("On: full Python (loops, if, variables).\n"
                                  "Off: the fast native path -- one primitive/macro call per line, no control flow.")
        head.addWidget(self.simplified_cb)
        head.addWidget(self.python_cb)
        rcol.addLayout(head)
        self.code = ZoomablePlainTextEdit()
        cpal = self.code.palette()
        cpal.setColor(QPalette.Base, theme.surface())
        cpal.setColor(QPalette.Text, theme.text())
        self.code.setPalette(cpal)
        rcol.addWidget(self.code, stretch=1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 900])

        # ------------------------------------------------------------ bottom bar
        bottom = QHBoxLayout()
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(label_style(theme.text()))
        bottom.addWidget(self.error, stretch=1)
        self.save_btn = CustomButton("Save")
        self.save_close_btn = CustomButton("Save and Close")
        self.close_btn = CustomButton("Close")
        self.save_btn.clicked.connect(self.save)
        self.save_close_btn.clicked.connect(self.save_and_close)
        self.close_btn.clicked.connect(self.request_close)
        for b in (self.save_btn, self.save_close_btn, self.close_btn):
            bottom.addWidget(b)
        root.addLayout(bottom)

    # ------------------------------------------------------------------ load / collect

    def load_macro(self, macro_id: str | None) -> None:
        existing = self.model.find(macro_id)
        self.macro = copy.deepcopy(existing) if existing else blank_macro()
        m = self.macro
        self.name_edit.setText(m.get("name", ""))
        self.desc_edit.setText(m.get("description", ""))
        mode = m.get("repeat_mode", "none")
        self.repeat.setCurrentIndex(REPEAT_MODES.index(mode) if mode in REPEAT_MODES else 0)
        self.edge.setCurrentIndex(1 if m.get("trigger_edge") == "up" else 0)
        self._set_combo_label(m.get("combo", []))
        self.ign_kb.setChecked(bool(m.get("ignore_keyboard")))
        self.ign_btn.setChecked(bool(m.get("ignore_mouse_buttons")))
        self.ign_move.setChecked(bool(m.get("ignore_mouse_movement")))
        self.simplified_cb.setChecked(bool(m.get("simplified_names", False)))
        self.python_cb.setChecked(bool(m.get("python_on", True)))
        self.code.setPlainText(m.get("code", ""))
        self.error.setText("")
        self.tr_status.setText("")

        for _e, _c, w in self._alias_rows:
            w.setParent(None)
            w.deleteLater()
        self._alias_rows = []
        aliases = cfg.load_aliases().get("aliases", {})
        for name, target in aliases.items():
            self._add_alias_row(name, target)
        if not aliases:
            self._add_alias_row()
        self._snapshot = self._fingerprint()

    def _collect(self) -> dict:
        m = dict(self.macro)
        m["name"] = self.name_edit.text().strip() or "Unnamed Macro"
        m["description"] = self.desc_edit.text().strip()
        m["repeat_mode"] = REPEAT_MODES[self.repeat.currentIndex()]
        m["trigger_edge"] = EDGES[self.edge.currentIndex()]
        m["code"] = self.code.toPlainText()
        m["simplified_names"] = self.simplified_cb.isChecked()
        m["python_on"] = self.python_cb.isChecked()
        m["ignore_keyboard"] = self.ign_kb.isChecked()
        m["ignore_mouse_buttons"] = self.ign_btn.isChecked()
        m["ignore_mouse_movement"] = self.ign_move.isChecked()
        return m

    def _collect_aliases(self) -> dict:
        out = {}
        for name_edit, target, _w in self._alias_rows:
            name = name_edit.text().strip()
            if name and target.currentText():
                out[name] = target.currentText()
        return out

    def _fingerprint(self) -> str:
        return json.dumps([self._collect(), self._collect_aliases()], sort_keys=True)

    def has_unsaved_changes(self) -> bool:
        return self._fingerprint() != self._snapshot

    # ------------------------------------------------------------------ save / close

    def save(self) -> bool:
        macro = self._collect()
        ok, msg = cfg.check_macro(macro)
        if not ok:
            self.error.setText(f"Code error, not saved: {msg}")
            return False
        cfg.save_aliases({"aliases": self._collect_aliases()})
        self.model.commit_macro(macro)
        self.macro = copy.deepcopy(self.model.find(macro.get("id")) or macro)
        self.model.save()
        self._snapshot = self._fingerprint()
        self.error.setText("Saved." + ("" if msg in ("OK", "") else f" ({msg})"))
        return True

    def save_and_close(self) -> None:
        if self.save():
            self._leave()

    def request_close(self) -> None:
        if self.has_unsaved_changes():
            choice = ask(self, "Unsaved changes", "Save them before closing?", ["Cancel", "Discard", "Save"], default=2)
            if choice == 2:
                self.save_and_close()
                return
            if choice != 1:
                return
        self._leave()

    def _leave(self) -> None:
        self.transcriber.stop()
        if self._recorder is not None:
            self._recorder.cancel()
        self.on_close()

    # ------------------------------------------------------------------ lifecycle

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._poller is None:
            self._poller = MousePositionPoller(self)
            self._poller.position.connect(self.mouse_pos.setText)
            self._poller.start()
        self._restart_hotkey_listener()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.stop_threads()

    def stop_threads(self) -> None:
        if self._poller is not None:
            self._poller.stop()
            self._poller.wait(1000)
            self._poller = None
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            self._hotkey_listener.wait(500)
            self._hotkey_listener = None
        self.transcriber.stop()

    def _restart_hotkey_listener(self) -> None:
        """The transcribe hotkey only does anything while this page is
        visible -- this listener is (re)started on showEvent and stopped
        on hideEvent, and restarted here too whenever the hotkey or the
        devices it watches change."""
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            self._hotkey_listener.wait(500)
            self._hotkey_listener = None
        code = resolve_key_code(self.model.state.get("transcribe_hotkey"))
        if code is None:
            return
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        if not kb and not mouse:
            return
        self._hotkey_listener = HotkeyListener(kb, mouse, code, self)
        self._hotkey_listener.pressed.connect(self._hotkey_pressed)
        self._hotkey_listener.start()

    def _hotkey_pressed(self) -> None:
        self._transcribe_clicked()

    # ------------------------------------------------------------------ combo

    def _set_combo_label(self, names: list) -> None:
        self.combo_label.setText(" + ".join(names) if names else "(none set)")

    def _record_clicked(self) -> None:
        if self._recorder is not None:
            self._recorder.cancel()
            return
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        if not kb and not mouse:
            self.combo_label.setText("Set a keyboard/mouse device in Settings first.")
            return
        self._recorder = ComboRecorder(kb, mouse, float(self.model.state.get("record_time_seconds", 3.0)), self)
        self._recorder.progress.connect(self._combo_progress)
        self._recorder.finished_combo.connect(self._combo_done)
        self.record_btn.setText("Recording… (click to cancel)")
        self._recorder.start()

    def _combo_progress(self, names: list, left: float) -> None:
        if not names:
            self.combo_label.setText("(listening… press and hold your combo)")
        elif left > 0:
            self.combo_label.setText(f"{' + '.join(names)} — hold steady ({left:.1f}s left)")
        else:
            self.combo_label.setText(f"{' + '.join(names)} — captured!")

    def _combo_done(self, names: list, status: str) -> None:
        rec, self._recorder = self._recorder, None
        if rec is not None:
            rec.wait(500)
        self.record_btn.setText("Record Combo")
        if status == "ok":
            self.macro["combo"] = names
        elif status == "no_devices":
            self.combo_label.setText("Couldn't open keyboard/mouse device -- check paths & permissions.")
            return
        self._set_combo_label(self.macro.get("combo", []))

    def _clear_combo(self) -> None:
        if self._recorder is None:
            self.macro["combo"] = []
            self._set_combo_label([])

    # ------------------------------------------------------------------ transcription

    def _transcribe_pref(self, key: str, on: bool) -> None:
        self.model.set_pref(key, on)
        self._sync_transcribe_enabled()

    def _sync_transcribe_enabled(self) -> None:
        mouse = self.tr_mouse.isChecked()
        if not mouse and self.tr_raw.isChecked():
            self.tr_raw.setChecked(False)
        raw = mouse and self.tr_raw.isChecked()
        self.tr_raw.setEnabled(mouse)
        for w in (self.tr_setpos, self.tr_samestart, self.tr_hz):
            w.setEnabled(raw)
        if not raw:
            for cb in (self.tr_setpos, self.tr_samestart):
                if cb.isChecked():
                    cb.setChecked(False)
        # Precise mode's per-frame recording only applies to raw motion,
        # but its precise=True waits apply to every line -- always available.

    def _transcribe_clicked(self) -> None:
        if self.transcriber.running():
            self.transcriber.stop()
            return
        kb_on, mouse_on = self.tr_kb.isChecked(), self.tr_mouse.isChecked()
        if not kb_on and not mouse_on:
            self.tr_status.setText("Check Transcribe Keyboard and/or Transcribe Mouse first.")
            return
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        if (kb_on and not kb) or (mouse_on and not mouse):
            self.tr_status.setText("Set a keyboard/mouse device in Settings first.")
            return
        err = self.transcriber.start(
            keyboard_path=kb, mouse_path=mouse, transcribe_keyboard=kb_on, transcribe_mouse=mouse_on,
            raw=self.tr_raw.isChecked(), set_positions=self.tr_setpos.isChecked(),
            same_start=self.tr_samestart.isChecked(), raw_hz=self.tr_hz.value(),
            precise=self.tr_precise.isChecked(), ping_key=self.ping_label.text(),
            abort_key=self.model.state.get("abort_key") or "KEY_PAUSE",
            hotkey_key=self.model.state.get("transcribe_hotkey") or None)
        if err:
            self.tr_status.setText(err)
            return
        self.tr_btn.setText("Stop Transcribing")
        self.tr_status.setText("Transcribing… inserting code at your cursor position.")

    def _insert_transcribed(self, text: str) -> None:
        self.code.textCursor().insertText(text)
        self.code.ensureCursorVisible()

    def _transcribe_stopped(self, msg: str) -> None:
        self.tr_btn.setText("Start Transcribing")
        self.tr_status.setText(msg)

    def _change_ping(self) -> None:
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        self.ping_btn.setEnabled(False)
        self.ping_label.setText("press a key…")
        self._ping_detect = DetectKey(kb, mouse, parent=self)
        self._ping_detect.found.connect(self._ping_found)
        self._ping_detect.start()

    def _ping_found(self, code, name) -> None:
        self.ping_btn.setEnabled(True)
        if code is None:
            self.ping_label.setText(self.model.state.get("transcribe_ping_key") or "KEY_INSERT")
            self.tr_status.setText("No key detected -- kept the previous ping key.")
            return
        self.ping_label.setText(name)
        self.model.set_pref("transcribe_ping_key", name)

    def _change_hotkey(self) -> None:
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        self.hotkey_btn.setEnabled(False)
        self.hotkey_label.setText("press a key…")
        self._hotkey_detect = DetectKey(kb, mouse, parent=self)
        self._hotkey_detect.found.connect(self._hotkey_found)
        self._hotkey_detect.start()

    def _hotkey_found(self, code, name) -> None:
        self.hotkey_btn.setEnabled(True)
        if code is None:
            self.hotkey_label.setText(self.model.state.get("transcribe_hotkey") or "(not set)")
            self.tr_status.setText("No key detected -- kept the previous transcribe hotkey.")
            return
        self.hotkey_label.setText(name)
        self.model.set_pref("transcribe_hotkey", name)
        self._restart_hotkey_listener()

    # ------------------------------------------------------------------ aliases

    def _add_alias_row(self, name: str = "", target: str | None = None) -> None:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        edit = CustomLineEdit(name)
        lay.addWidget(edit, stretch=1)
        lay.addWidget(QLabel("="))
        combo = QComboBox()
        combo.setStyleSheet(combo_box_stylesheet(theme_config.get_settings()))
        targets = alias_targets()
        combo.addItems(targets)
        if target in targets:
            combo.setCurrentText(target)
        lay.addWidget(combo)
        remove = CustomButton("Remove")
        lay.addWidget(remove)
        entry = (edit, combo, w)
        remove.clicked.connect(lambda: self._remove_alias_row(entry))
        self.alias_box.addWidget(w)
        self._alias_rows.append(entry)

    def _remove_alias_row(self, entry) -> None:
        if entry in self._alias_rows:
            self._alias_rows.remove(entry)
            entry[2].setParent(None)
            entry[2].deleteLater()
