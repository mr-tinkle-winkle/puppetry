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
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeyEvent, QPalette, QTextCursor, QWheelEvent
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

import block_model as bm
import puppetry_config as cfg
import sound
import custom_blocks
from block_editor import BlockEditor
from custom_block_dialog import CustomBlockDialog
from input_tools import ComboRecorder, DetectKey, HotkeyListener, MousePositionPoller, resolve_key_code
from model import AppModel
from reference import alias_targets
from transcription import TranscriptionController
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.custom_spinbox import CustomDoubleSpinBox
from ui_kit.segment_button import SegmentButton
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme
from widgets import Collapsible, ask, dim_label, label_style, mark_disable, mark_input, mark_output, section_title

REPEAT_MODES = ["none", "hold", "toggle"]
REPEAT_LABELS = ["No Repeat", "Hold", "Toggle"]
EDGES = ["down", "up"]
EDGE_LABELS = ["On Press", "On Release"]
VIEW_BLOCKS, VIEW_TEXT = "blocks", "text"


def default_view_mode(state: dict) -> str:
    """The app-wide default (Settings > Behavior). Blocks unless set."""
    return VIEW_TEXT if state.get("editor_default_mode") == VIEW_TEXT else VIEW_BLOCKS


def sanitize_macro_name(name: str) -> str:
    """Same rule as native/src/macro.cpp's sanitize_macro_name()."""
    ident = "".join(c if (c.isalnum() and c.isascii()) or c == "_" else "_" for c in (name or "macro"))
    if not ident or ident[0].isdigit():
        ident = "m_" + ident
    return ident


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
        self.open_dictionary = lambda: None      # set by MainWindow (jumps to the Dictionary page)
        self.macro: dict = blank_macro()
        self._snapshot = ""
        self._recorder: ComboRecorder | None = None
        self._ping_detect: DetectKey | None = None
        self._hotkey_detect: DetectKey | None = None
        self._restart_key_detect: DetectKey | None = None
        self._checkpoint_key_detect: DetectKey | None = None
        self._poller: MousePositionPoller | None = None
        self._hotkey_listener: HotkeyListener | None = None
        self._restart_key_listener: HotkeyListener | None = None
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
        self.left_scroll = left_scroll
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

        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("Category"))
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.setStyleSheet(css)
        self.category.setToolTip("Group this macro on the Macros page. Type a new name to make a new category; "
                                 "leave it empty for Uncategorized.")
        self.category.lineEdit().setPlaceholderText("Uncategorized")
        cat_row.addWidget(self.category, stretch=1)
        col.addLayout(cat_row)

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
        mark_disable(self.clear_combo_btn)
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
        self.transcribe_title = section_title("Transcribe Inputs")
        col.addWidget(self.transcribe_title)
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
        self.tr_clear = CustomCheckBox("Clear macro before transcribing")
        self.tr_clear.setToolTip("Wipes the code box every time you click Start Transcribing (or press the "
                                 "transcribe hotkey), so each session starts from a blank macro instead of "
                                 "appending to whatever was already there.")
        self.tr_ignore_puppetry = CustomCheckBox("Ignore Puppetry")
        self.tr_ignore_puppetry.setToolTip("Stops listening entirely while Puppetry's own window is focused -- "
                                           "so switching over here to check something doesn't get typed/clicked "
                                           "into the macro. Time spent focused on Puppetry doesn't count towards "
                                           "the next wait() either.")
        self.tr_ignore_alttab = CustomCheckBox("Ignore Alt+Tab")
        self.tr_ignore_alttab.setToolTip("Drops Alt+Tab (both keys) from the transcript entirely -- switching "
                                        "windows mid-recording won't leave it in the macro.")
        for cb in (self.tr_kb, self.tr_mouse, self.tr_raw, self.tr_setpos, self.tr_samestart, self.tr_precise,
                   self.tr_clear, self.tr_ignore_puppetry, self.tr_ignore_alttab):
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
        restart_row = QHBoxLayout()
        restart_row.addWidget(QLabel("Restart hotkey"))
        self.restart_label = QLabel()
        self.restart_label.setFont(QFont("monospace"))
        restart_row.addWidget(self.restart_label, stretch=1)
        self.restart_btn = CustomButton("Change")
        self.restart_btn.clicked.connect(self._change_restart_key)
        restart_row.addWidget(self.restart_btn)
        col.addLayout(restart_row)
        col.addWidget(dim_label("Separate from the toggle hotkey above: only does anything while a "
                                "transcription is already running, and then it stops it and starts a fresh "
                                "one right away, rather than alternating start/stop -- handy for redoing a "
                                "take without reaching for the mouse. Does nothing if nothing's recording. "
                                "Never itself recorded."))
        checkpoint_row = QHBoxLayout()
        checkpoint_row.addWidget(QLabel("Checkpoint key"))
        self.checkpoint_label = QLabel()
        self.checkpoint_label.setFont(QFont("monospace"))
        checkpoint_row.addWidget(self.checkpoint_label, stretch=1)
        self.checkpoint_btn = CustomButton("Change")
        self.checkpoint_btn.clicked.connect(self._change_checkpoint_key)
        checkpoint_row.addWidget(self.checkpoint_btn)
        col.addLayout(checkpoint_row)
        col.addWidget(dim_label("Inserts a checkpoint() line -- it does nothing when the macro runs, but "
                                "\"Clear macro before transcribing\" above will only clear what comes AFTER "
                                "the last checkpoint() instead of the whole macro, and the next recording "
                                "starts right after it. Drop one in by hand, or press this key while "
                                "transcribing."))

        # Audible cues: handy when transcription is started/stopped by the
        # hotkey from another window, where there's no status text to see.
        self.sound_labels: dict[str, QLabel] = {}
        self.sound_test_btns: list = []
        for key, caption in (("start", "Start sound"), ("finish", "Finish sound")):
            row = QHBoxLayout()
            row.addWidget(QLabel(caption))
            lbl = QLabel()
            lbl.setFont(QFont("monospace"))
            lbl.setMinimumWidth(10)
            row.addWidget(lbl, stretch=1)
            self.sound_labels[key] = lbl
            pick = CustomButton("Choose…")
            pick.clicked.connect(lambda _=False, k=key: self._choose_sound(k))
            row.addWidget(pick)
            test = CustomButton("▶")
            test.setToolTip("Play it now")
            test.clicked.connect(lambda _=False, k=key: self._test_sound(k))
            self.sound_test_btns.append(test)
            row.addWidget(test)
            clear = CustomButton("✕")
            clear.setToolTip("No sound")
            mark_disable(clear)
            clear.clicked.connect(lambda _=False, k=key: self._set_sound(k, ""))
            row.addWidget(clear)
            col.addLayout(row)
        col.addWidget(dim_label("Optional. Played when transcription starts and when it stops -- however it "
                                "stops (the button, the hotkey, the abort key, a disconnected device). "
                                + sound.describe_players()))

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
        self.tr_clear.setChecked(bool(st.get("transcribe_clear_before", False)))
        self.tr_ignore_puppetry.setChecked(bool(st.get("transcribe_ignore_puppetry", False)))
        self.tr_ignore_alttab.setChecked(bool(st.get("transcribe_ignore_alttab", False)))
        self.tr_hz.setValue(float(st.get("transcribe_raw_hz", 60)))
        self.ping_label.setText(st.get("transcribe_ping_key") or "KEY_INSERT")
        self.hotkey_label.setText(st.get("transcribe_hotkey") or "(not set)")
        self.restart_label.setText(st.get("transcribe_restart_key") or "(not set)")
        self.checkpoint_label.setText(st.get("transcribe_checkpoint_key") or "(not set)")
        for key in ("start", "finish"):
            self._show_sound(key)
        self._sync_transcribe_enabled()
        for cb, key in ((self.tr_kb, "transcribe_keyboard"), (self.tr_mouse, "transcribe_mouse"),
                        (self.tr_raw, "transcribe_raw"), (self.tr_setpos, "transcribe_setpos"),
                        (self.tr_samestart, "transcribe_samestart"), (self.tr_precise, "transcribe_precise"),
                        (self.tr_clear, "transcribe_clear_before"), (self.tr_ignore_puppetry, "transcribe_ignore_puppetry"),
                        (self.tr_ignore_alttab, "transcribe_ignore_alttab")):
            cb.toggled.connect(lambda on, key=key: self._transcribe_pref(key, on))
        self.tr_hz.valueChanged.connect(lambda v: model.set_pref("transcribe_raw_hz", v))
        self.tr_setpos.toggled.connect(lambda on: on and self.tr_samestart.setChecked(False))
        self.tr_samestart.toggled.connect(lambda on: on and self.tr_setpos.setChecked(False))
        # input/output color scheme: everything here that READS real input
        # is orange; the sound previews (Puppetry making noise) are blue.
        for w in (self.record_btn, self.ping_btn, self.hotkey_btn, self.restart_btn, self.checkpoint_btn,
                  self.tr_btn, self.mouse_pos, self.transcribe_title):
            mark_input(w)
        for b in self.sound_test_btns:
            mark_output(b)
        self.transcriber.text_ready.connect(self._insert_transcribed)
        self.transcriber.stopped.connect(self._transcribe_stopped)

        # ------------------------------------------------------------ ignore
        self.ignore_title = section_title("While this macro runs")
        mark_input(self.ignore_title)
        col.addWidget(self.ignore_title)
        self.ign_kb = CustomCheckBox("Ignore Keyboard Input (except Abort)")
        self.ign_btn = CustomCheckBox("Ignore Mouse Buttons")
        self.ign_move = CustomCheckBox("Ignore Mouse Movement")
        for cb in (self.ign_kb, self.ign_btn, self.ign_move):
            col.addWidget(cb)
        col.addWidget(dim_label("Actually grabs the real device while this macro runs, so your own input can't "
                                "interfere. The abort key always force-releases these if something gets stuck."))

        # ------------------------------------------------------------ references + aliases
        dict_btn = CustomButton("Open the Dictionary")
        dict_btn.setToolTip("Every command, block and key name, searchable.")
        dict_btn.clicked.connect(lambda: self.open_dictionary())
        dict_row = QHBoxLayout()
        dict_row.addWidget(dict_btn)
        dict_row.addStretch(1)
        col.addLayout(dict_row)

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
        head.setSpacing(0)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.blocks_view_btn = SegmentButton(text="Blocks", position="left")
        self.text_view_btn = SegmentButton(text="Text", position="right")
        for b in (self.blocks_view_btn, self.text_view_btn):
            b.setMinimumHeight(32)
            b.setMinimumWidth(84)
            head.addWidget(b)
        self.blocks_view_btn.setToolTip("Edit this macro as snap-together blocks.\n"
                                        "Same code underneath -- switch back and forth any time.\n"
                                        "(Which view macros open in: Settings > Behavior.)")
        self.text_view_btn.setToolTip("Edit this macro's code as text.")
        self.view_group.addButton(self.blocks_view_btn, 0)
        self.view_group.addButton(self.text_view_btn, 1)
        self.view_group.idClicked.connect(lambda i: self.set_view_mode(VIEW_BLOCKS if i == 0 else VIEW_TEXT))
        head.addSpacing(pad)
        head.addStretch(1)
        self.simplified_cb = CustomCheckBox("Simplified Variable Names")
        self.python_cb = CustomCheckBox("Run as embedded Python")
        self.python_cb.setToolTip("On: real embedded Python -- anything goes, imports included.\n"
                                  "Off: Puppetry's fast native interpreter -- variables, if/else, loops, functions,\n"
                                  "math, every block; no imports, classes, try or methods like \"a\".upper().")
        head.addWidget(self.simplified_cb)
        head.addWidget(self.python_cb)
        rcol.addLayout(head)
        self.code = ZoomablePlainTextEdit()
        cpal = self.code.palette()
        cpal.setColor(QPalette.Base, theme.surface())
        cpal.setColor(QPalette.Text, theme.text())
        self.code.setPalette(cpal)
        self.blocks = BlockEditor()
        self.blocks.custom_dialog_factory = CustomBlockDialog
        self.blocks.customs_changed.connect(self._customs_changed)
        self.simplified_cb.toggled.connect(lambda _on: self._refresh_block_context())
        self.code_stack = QStackedWidget()
        self.code_stack.addWidget(self.blocks)
        self.code_stack.addWidget(self.code)
        rcol.addWidget(self.code_stack, stretch=1)
        # Block view state: the text the canvas was last synced with, and
        # the canvas revision at that moment. If neither side changed, a
        # switch is free and lossless (no parse, no regeneration).
        self._view_mode = VIEW_TEXT
        self._blocks_synced_text: str | None = None
        self._blocks_synced_rev = -1
        self._reparse_timer = QTimer(self)
        self._reparse_timer.setSingleShot(True)
        self._reparse_timer.setInterval(250)
        self._reparse_timer.timeout.connect(self._reparse_after_transcription)
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

        # Matches the actual (empty) widget state above, not blank_macro()'s
        # defaults -- load_macro() hasn't run yet at construction time, and
        # without this, has_unsaved_changes() would report unsaved changes
        # before the editor has ever been opened (app.py now checks it on
        # every open_editor() call, to preserve in-progress edits when
        # re-opening the macro that's already loaded).
        self._snapshot = self._fingerprint()

    # ------------------------------------------------------------------ load / collect

    def load_macro(self, macro_id: str | None) -> None:
        existing = self.model.find(macro_id)
        self.macro = copy.deepcopy(existing) if existing else blank_macro()
        m = self.macro
        self.name_edit.setText(m.get("name", ""))
        self.desc_edit.setText(m.get("description", ""))
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItems([""] + self.model.category_names())
        self.category.setCurrentText(m.get("category") or "")
        self.category.blockSignals(False)
        mode = m.get("repeat_mode", "none")
        self.repeat.setCurrentIndex(REPEAT_MODES.index(mode) if mode in REPEAT_MODES else 0)
        self.edge.setCurrentIndex(1 if m.get("trigger_edge") == "up" else 0)
        self._set_combo_label(m.get("combo", []))
        self.ign_kb.setChecked(bool(m.get("ignore_keyboard")))
        self.ign_btn.setChecked(bool(m.get("ignore_mouse_buttons")))
        self.ign_move.setChecked(bool(m.get("ignore_mouse_movement")))
        self.simplified_cb.setChecked(bool(m.get("simplified_names", False)))
        self._loading = True
        self.python_cb.setChecked(bool(m.get("python_on", True)))
        self._loading = False
        self.code.setPlainText(m.get("code", ""))
        self.error.setText("")
        self.tr_status.setText("")
        self._blocks_synced_text = None
        self._view_mode = VIEW_TEXT
        self._refresh_block_context()
        self.set_view_mode(default_view_mode(self.model.state), quiet=True)

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
        cat = self.category.currentText().strip()
        if cat:
            m["category"] = cat
        else:
            m.pop("category", None)
        m["repeat_mode"] = REPEAT_MODES[self.repeat.currentIndex()]
        m["trigger_edge"] = EDGES[self.edge.currentIndex()]
        m["code"] = self.code_text()
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
        if macro.get("category") and self.model.category(macro["category"]) is None:
            self.model.categories().append({"name": macro["category"], "enabled": True})
        self.model.commit_macro(macro)
        self.macro = copy.deepcopy(self.model.find(macro.get("id")) or macro)
        self.model.save()
        self._snapshot = self._fingerprint()
        note = ""
        if self._view_mode == VIEW_BLOCKS and self.blocks.loose_count():
            n = self.blocks.loose_count()
            note = f" {n} loose block{'s' if n != 1 else ''} on the canvas aren't attached, so weren't saved."
        self.error.setText("Saved." + ("" if msg in ("OK", "") else f" ({msg})") + note)
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
        self._rearm_restart_key_listener()

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
        if self._restart_key_listener is not None:
            self._restart_key_listener.stop()
            self._restart_key_listener.wait(500)
            self._restart_key_listener = None
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

    def _rearm_restart_key_listener(self) -> None:
        """Same lifecycle as _restart_hotkey_listener() above, but for the
        SEPARATE restart-transcription key -- its own listener, watching
        its own code, independent of the toggle hotkey's listener."""
        if self._restart_key_listener is not None:
            self._restart_key_listener.stop()
            self._restart_key_listener.wait(500)
            self._restart_key_listener = None
        code = resolve_key_code(self.model.state.get("transcribe_restart_key"))
        if code is None:
            return
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        if not kb and not mouse:
            return
        self._restart_key_listener = HotkeyListener(kb, mouse, code, self)
        self._restart_key_listener.pressed.connect(self._restart_key_pressed)
        self._restart_key_listener.start()

    def _restart_key_pressed(self) -> None:
        self._restart_transcription()

    # ------------------------------------------------------------------ combo

    def _set_combo_label(self, names: list) -> None:
        self.combo_label.setText(" + ".join(names) if names else "(none set)")
        if hasattr(self, "blocks"):
            self.blocks.set_context(hat_text=self._hat_text(names))

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
        self._start_transcription()

    def _restart_transcription(self) -> None:
        """The restart hotkey: only does anything while a transcription is
        already running -- a no-op otherwise, it never itself starts one
        from a standing stop. While one's active, it stops it and starts a
        fresh session right away -- same clear-before-transcribing/checkpoint
        handling, sounds and all, as a normal Start. Separate from the
        toggle hotkey on purpose: that one alternates start/stop from either
        state, this one only ever restarts an in-progress recording."""
        if not self.transcriber.running():
            return
        self.transcriber.stop()
        self._start_transcription()

    def _apply_clear_before_transcribing(self) -> None:
        """"Clear macro before transcribing": normally wipes the whole
        code box. But if a checkpoint() line is anywhere in the code, that
        line and everything before it survive -- only what comes AFTER the
        LAST checkpoint() is cleared -- and the cursor moves to right after
        it, so the new recording starts there instead of wherever the
        cursor happened to be. checkpoint() itself does nothing when the
        macro runs (see its docstring); this is its only purpose."""
        if not self.tr_clear.isChecked():
            return
        text = self.code.toPlainText()
        idx = text.rfind("checkpoint()")
        if idx == -1:
            keep = ""
        else:
            end = text.find("\n", idx)
            keep = text[:end + 1] if end != -1 else text
        # an undoable edit (setPlainText would wipe the undo history), and
        # the first piece of this transcription's single undo step
        cursor = QTextCursor(self.code.document())
        cursor.beginEditBlock()
        cursor.setPosition(len(keep))
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.endEditBlock()
        self._tr_join = True
        end_cursor = self.code.textCursor()
        end_cursor.movePosition(QTextCursor.MoveOperation.End)
        self.code.setTextCursor(end_cursor)

    def _start_transcription(self) -> None:
        kb_on, mouse_on = self.tr_kb.isChecked(), self.tr_mouse.isChecked()
        if not kb_on and not mouse_on:
            self.tr_status.setText("Check Transcribe Keyboard and/or Transcribe Mouse first.")
            return
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        if (kb_on and not kb) or (mouse_on and not mouse):
            self.tr_status.setText("Set a keyboard/mouse device in Settings first.")
            return
        # The whole transcription (clearing included) is ONE undo step, in
        # either view.
        self._tr_join = False
        if self._view_mode == VIEW_BLOCKS:
            # Transcription writes text; in Block view it goes to the end of
            # the macro (there's no visible text cursor) and the blocks are
            # re-read from it as lines arrive.
            self.code_text()
            self.blocks.begin_external_edit()
            cursor = self.code.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.code.setTextCursor(cursor)
        self._apply_clear_before_transcribing()
        if self._view_mode == VIEW_BLOCKS:
            self._reparse_timer.start()
        err = self.transcriber.start(
            keyboard_path=kb, mouse_path=mouse, transcribe_keyboard=kb_on, transcribe_mouse=mouse_on,
            raw=self.tr_raw.isChecked(), set_positions=self.tr_setpos.isChecked(),
            same_start=self.tr_samestart.isChecked(), raw_hz=self.tr_hz.value(),
            precise=self.tr_precise.isChecked(), ping_key=self.ping_label.text(),
            abort_key=self.model.state.get("abort_key") or "KEY_PAUSE",
            hotkey_key=self.model.state.get("transcribe_hotkey") or None,
            restart_key=self.model.state.get("transcribe_restart_key") or None,
            checkpoint_key=self.model.state.get("transcribe_checkpoint_key") or None,
            ignore_alt_tab=self.tr_ignore_alttab.isChecked(),
            ignore_puppetry=self.tr_ignore_puppetry.isChecked())
        if err:
            self.tr_status.setText(err)
            return
        self.tr_btn.setText("Stop Transcribing")
        self.tr_status.setText("Transcribing… inserting code at your cursor position.")
        self._play_sound("start")

    def _insert_transcribed(self, text: str) -> None:
        if self._view_mode == VIEW_BLOCKS:
            # no visible text cursor in Block view: always append
            cursor = self.code.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.code.setTextCursor(cursor)
        cursor = self.code.textCursor()
        if getattr(self, "_tr_join", False):
            cursor.joinPreviousEditBlock()
        else:
            cursor.beginEditBlock()
        cursor.insertText(text)
        cursor.endEditBlock()
        self._tr_join = True
        self.code.setTextCursor(cursor)
        self.code.ensureCursorVisible()
        if self._view_mode == VIEW_BLOCKS:
            self._reparse_timer.start()

    def _transcribe_stopped(self, msg: str) -> None:
        if self._view_mode == VIEW_BLOCKS and self._reparse_timer.isActive():
            self._reparse_timer.stop()
            self._reparse_after_transcription()
        self.tr_btn.setText("Start Transcribing")
        self.tr_status.setText(msg)
        self._play_sound("finish")

    # -- transcription start/finish sounds
    def _sound_key(self, which: str) -> str:
        return f"transcribe_{which}_sound"

    def _show_sound(self, which: str) -> None:
        path = self.model.state.get(self._sound_key(which)) or ""
        label = self.sound_labels[which]
        label.setText(os.path.basename(path) if path else "(none)")
        label.setToolTip(path)

    def _set_sound(self, which: str, path: str) -> None:
        self.model.set_pref(self._sound_key(which), path)
        self._show_sound(which)

    def _choose_sound(self, which: str) -> None:
        start_dir = os.path.dirname(self.model.state.get(self._sound_key(which)) or "") or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, f"Sound to play when transcription {'starts' if which == 'start' else 'stops'}",
                                              start_dir, sound.AUDIO_FILTER)
        if path:
            self._set_sound(which, path)

    def _test_sound(self, which: str) -> None:
        path = self.model.state.get(self._sound_key(which)) or ""
        if not path:
            self.tr_status.setText(f"No {which} sound chosen yet.")
            return
        err = sound.play(path)
        if err:
            self.tr_status.setText(err)

    def _play_sound(self, which: str) -> None:
        # A cue that can't play is worth saying once, but never worth
        # interrupting transcription over.
        err = sound.play(self.model.state.get(self._sound_key(which)) or "")
        if err:
            self.tr_status.setText(err)

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

    def _change_restart_key(self) -> None:
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        self.restart_btn.setEnabled(False)
        self.restart_label.setText("press a key…")
        self._restart_key_detect = DetectKey(kb, mouse, parent=self)
        self._restart_key_detect.found.connect(self._restart_key_found)
        self._restart_key_detect.start()

    def _restart_key_found(self, code, name) -> None:
        self.restart_btn.setEnabled(True)
        if code is None:
            self.restart_label.setText(self.model.state.get("transcribe_restart_key") or "(not set)")
            self.tr_status.setText("No key detected -- kept the previous restart key.")
            return
        self.restart_label.setText(name)
        self.model.set_pref("transcribe_restart_key", name)
        self._rearm_restart_key_listener()

    def _change_checkpoint_key(self) -> None:
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        self.checkpoint_btn.setEnabled(False)
        self.checkpoint_label.setText("press a key…")
        self._checkpoint_key_detect = DetectKey(kb, mouse, parent=self)
        self._checkpoint_key_detect.found.connect(self._checkpoint_key_found)
        self._checkpoint_key_detect.start()

    def _checkpoint_key_found(self, code, name) -> None:
        self.checkpoint_btn.setEnabled(True)
        if code is None:
            self.checkpoint_label.setText(self.model.state.get("transcribe_checkpoint_key") or "(not set)")
            self.tr_status.setText("No key detected -- kept the previous checkpoint key.")
            return
        self.checkpoint_label.setText(name)
        self.model.set_pref("transcribe_checkpoint_key", name)

    # ------------------------------------------------------------------ blocks / text view

    def _hat_text(self, combo=None) -> str:
        names = self.macro.get("combo", []) if combo is None else combo
        return f"when {' + '.join(names)} pressed" if names else "when this macro runs"

    def _refresh_block_context(self) -> None:
        mine = self.macro.get("id")
        names = [sanitize_macro_name(m.get("name", "")) for m in self.model.macros() if m.get("id") != mine]
        tables = cfg.name_tables()
        keys = list(tables.get("keys", []))
        if self.simplified_cb.isChecked():
            keys += list(tables.get("simplified", {}).keys())
        self.blocks.set_context(macro_names=sorted(set(names)), hat_text=self._hat_text(), key_names=keys,
                                customs=custom_blocks.load())

    def _customs_changed(self) -> None:
        """A custom block was created/edited/deleted (already on disk): the
        daemon registers custom blocks at startup, so restart it now."""
        ok, msg = cfg.restart_daemon_service()
        self.error.setText("Custom blocks saved." + ("" if ok else f" ({msg})"))
        self.blocks.revision += 1   # custom block calls may render differently now

    def view_mode(self) -> str:
        return self._view_mode

    def code_text(self) -> str:
        """The macro's code, whichever view is showing. In Block view the
        text is regenerated from the blocks ONLY if they were edited since
        the last sync -- so just looking at a macro as blocks never
        rewrites it."""
        if self._view_mode == VIEW_BLOCKS and self.blocks.revision != self._blocks_synced_rev:
            text = self.blocks.code()
            if text != self.code.toPlainText():
                self.code.setPlainText(text)
            self._blocks_synced_text = text
            self._blocks_synced_rev = self.blocks.revision
        return self.code.toPlainText()

    def set_view_mode(self, mode: str, quiet: bool = False) -> bool:
        """Switch this macro between Blocks and Text. Returns False (and
        stays in Text) if the code can't be shown as blocks."""
        if mode == VIEW_BLOCKS:
            text = self.code_text()
            if self._view_mode != VIEW_BLOCKS or self._blocks_synced_text != text:
                if self._blocks_synced_text != text:
                    try:
                        doc = bm.code_to_blocks(text, self.blocks.macro_names, self.blocks.customs)
                    except bm.BlockParseError as exc:
                        self._view_mode = VIEW_TEXT
                        self._sync_view_buttons()
                        self.error.setText(f"Showing Text: this code can't be shown as blocks until "
                                           f"its syntax error is fixed ({exc}).")
                        return False
                    old = self.blocks.doc
                    doc.main_x, doc.main_y = old.main_x, old.main_y
                    self.blocks.set_doc(doc)
                    self._blocks_synced_text = text
                self._blocks_synced_rev = self.blocks.revision
            self._view_mode = VIEW_BLOCKS
        else:
            self.code_text()
            self._view_mode = VIEW_TEXT
        self._sync_view_buttons()
        if not quiet and self.error.text().startswith("Showing Text"):
            self.error.setText("")
        return True

    def _sync_view_buttons(self) -> None:
        blocks = self._view_mode == VIEW_BLOCKS
        self.blocks_view_btn.setChecked(blocks)
        self.text_view_btn.setChecked(not blocks)
        self.code_stack.setCurrentWidget(self.blocks if blocks else self.code)

    def _reparse_after_transcription(self) -> None:
        if self._view_mode != VIEW_BLOCKS:
            return
        text = self.code.toPlainText()
        try:
            doc = bm.code_to_blocks(text, self.blocks.macro_names, self.blocks.customs)
        except bm.BlockParseError:
            return
        old = self.blocks.doc
        doc.main_x, doc.main_y = old.main_x, old.main_y
        where = {f.name: (f.x, f.y) for f in old.functions}
        for f in doc.functions:
            f.x, f.y = where.get(f.name, (0, 0))
        # keep_undo: the snapshot taken when transcription started undoes it all
        self.blocks.set_doc(doc, reset_view=False, keep_undo=True)
        self._blocks_synced_text = text
        self._blocks_synced_rev = self.blocks.revision

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
        mark_disable(remove)
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
