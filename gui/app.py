"""
Puppetry's Qt (PySide6) editor -- replaces macro_gui.py's GTK4 app.
Built on the shared ui_kit per UI_THEMING_GUIDE.md; talks to the new
C++ daemon ONLY through the control socket (puppetry_config.py's
send_control_command()) and shared on-disk JSON config, exactly like
the old GTK app talked to the old Python daemon.

Feature scope this session (see the project handoff for the rest):
macro list, macro editor (name/code/repeat-mode/trigger-edge/ignore
checkboxes/simplified-names/python_on), lock toggle, Record Time
spinner, combo recorder (PAUSE/RESUME wired to the control socket),
code-editor Ctrl+Scroll zoom. NOT built yet: the Dictionary/Function
Reference window, Scratch-style block editing, and the eventual Qt
port's whole-app zoom -- all explicitly deferred in the existing
project handoff already, not new scope cut this session.

CLI mode: `puppetry --name="Macro Name" [args...]` / `puppetry --abort`
bypass the GUI entirely and just hit the control socket, same contract
macro_daemon.py's control socket has always offered.
"""
from __future__ import annotations

import sys
from dataclasses import asdict

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QPalette, QColor, QKeyEvent, QWheelEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QStackedWidget, QButtonGroup, QPlainTextEdit, QComboBox, QMessageBox,
)

import puppetry_config as cfg
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.custom_group_box import CustomGroupBox
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.custom_spinbox import CustomDoubleSpinBox
from ui_kit.page_outline import paint_page_outline
from ui_kit.scale_reveal import crossfade_to_index
from ui_kit.segment_button import SegmentButton
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme, contrast_text
from combo_recorder import ComboRecorder

REPEAT_MODES = ["none", "hold", "toggle"]
TRIGGER_EDGES = ["down", "up"]


def install_theme_provider() -> None:
    theme_config.set_settings_provider(cfg.load_theme_settings)


def apply_window_palette(widget: QWidget, theme: Theme) -> None:
    """Step 4 of the wiring checklist: background + text roles via
    QPalette, never setStyleSheet("background: ...") (pitfall #2), and
    WindowText/Text/ButtonText set to theme.text() so plain QLabels
    stay readable regardless of the system palette (pitfall #3)."""
    pal = widget.palette()
    pal.setColor(QPalette.Window, theme.app_background())
    pal.setColor(QPalette.Base, theme.page_background())
    pal.setColor(QPalette.WindowText, theme.text())
    pal.setColor(QPalette.Text, theme.text())
    pal.setColor(QPalette.ButtonText, theme.text())
    widget.setPalette(pal)
    widget.setAutoFillBackground(True)


class LockToggle(CustomButton):
    """🔒/🔓 toggle next to a macro's combo controls -- locking disables
    Edit/Delete/combo-recording/the trigger-edge dropdown until
    unlocked. Text-based (no bundled lock icon asset shipped with this
    kit copy yet), circular per the guide's icon-button pattern."""

    def __init__(self, locked: bool = False, parent=None):
        super().__init__("", parent)
        self.setCheckable(True)
        self.setChecked(locked)
        self.set_circular(28)
        self._sync_text()
        self.toggled.connect(self._sync_text)

    def _sync_text(self) -> None:
        self.setText("\U0001F512" if self.isChecked() else "\U0001F513")  # lock / unlock


class ZoomablePlainTextEdit(QPlainTextEdit):
    """The macro code editor. Ctrl+Scroll zooms THIS widget's font size
    only (not the whole app -- whole-app zoom is deferred to the Qt-era
    GUI's own planned pass, per the handoff). Ctrl+0 resets."""

    _DEFAULT_POINT_SIZE = 11
    _MIN_POINT_SIZE = 6
    _MAX_POINT_SIZE = 36

    def __init__(self, parent=None):
        super().__init__(parent)
        f = self.font()
        f.setFamily("monospace")
        f.setPointSize(self._DEFAULT_POINT_SIZE)
        self.setFont(f)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = 1 if event.angleDelta().y() > 0 else -1
            f = self.font()
            new_size = max(self._MIN_POINT_SIZE, min(self._MAX_POINT_SIZE, f.pointSize() + delta))
            f.setPointSize(new_size)
            self.setFont(f)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_0:
            f = self.font()
            f.setPointSize(self._DEFAULT_POINT_SIZE)
            self.setFont(f)
            event.accept()
            return
        super().keyPressEvent(event)


class PageBase(QWidget):
    """Every top-level page: its own background, SmoothScrollArea inside
    it, a page outline, and autoFillBackground turned back off on the
    scroll area + its viewport (pitfall #5)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        theme = Theme()
        self._bg = theme.page_background()
        self.setAutoFillBackground(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 0)  # room for the 3px outline (section 3 of the guide)

        self.scroll = SmoothScrollArea(self)
        self.scroll.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setWidgetResizable(True)

        self.content = QWidget()
        self.content.setAutoFillBackground(False)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(theme.padding, theme.padding, theme.padding, theme.padding)
        self.content_layout.setSpacing(theme.padding)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._bg)
        paint_page_outline(self, self._bg, skip_top=True)


class MacroRow(QWidget):
    """One macro in the list: name + combo summary, Edit/Delete, and the
    lock toggle. Uses CustomButton for the row's own actions."""

    def __init__(self, macro_def: dict, on_edit, on_delete, on_lock_changed, parent=None):
        super().__init__(parent)
        self.macro_id = macro_def.get("id")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        theme = Theme()
        name_label = QLabel(macro_def.get("name", "(unnamed)"))
        name_label.setStyleSheet(f"QLabel {{ color: {theme.text().name()}; }}")
        combo_label = QLabel(" + ".join(macro_def.get("combo", [])) or "(no combo)")
        combo_label.setStyleSheet(f"QLabel {{ color: {theme.text().name()}; }}")

        edit_btn = CustomButton("Edit")
        edit_btn.clicked.connect(lambda: on_edit(self.macro_id))
        delete_btn = CustomButton("Delete")
        delete_btn.clicked.connect(lambda: on_delete(self.macro_id))
        self.lock = LockToggle(locked=macro_def.get("locked", False))
        self.lock.toggled.connect(lambda checked: on_lock_changed(self.macro_id, checked))

        edit_btn.setEnabled(not self.lock.isChecked())
        delete_btn.setEnabled(not self.lock.isChecked())
        self.lock.toggled.connect(lambda checked: (edit_btn.setEnabled(not checked), delete_btn.setEnabled(not checked)))

        layout.addWidget(name_label)
        layout.addWidget(combo_label)
        layout.addStretch(1)
        layout.addWidget(edit_btn)
        layout.addWidget(delete_btn)
        layout.addWidget(self.lock)


class MacroListPage(PageBase):
    def __init__(self, on_edit, on_new, parent=None):
        super().__init__(parent)
        self._on_edit = on_edit

        new_btn = CustomButton("+ New Macro")
        new_btn.clicked.connect(on_new)
        self.content_layout.addWidget(new_btn)

        self.rows_container = QVBoxLayout()
        self.content_layout.addLayout(self.rows_container)
        self.content_layout.addStretch(1)

        self.refresh()

    def refresh(self) -> None:
        while self.rows_container.count():
            item = self.rows_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        macros_doc = cfg.load_macros()
        for macro_def in macros_doc.get("macros", []):
            row = MacroRow(macro_def, self._on_edit, self._delete_macro, self._set_locked)
            self.rows_container.addWidget(row)

    def _delete_macro(self, macro_id: str) -> None:
        macros_doc = cfg.load_macros()
        macros_doc["macros"] = [m for m in macros_doc.get("macros", []) if m.get("id") != macro_id]
        cfg.save_macros(macros_doc)
        cfg.restart_daemon_service()
        self.refresh()

    def _set_locked(self, macro_id: str, locked: bool) -> None:
        macros_doc = cfg.load_macros()
        for m in macros_doc.get("macros", []):
            if m.get("id") == macro_id:
                m["locked"] = locked
        cfg.save_macros(macros_doc)


class MacroEditorPage(PageBase):
    """Editor for one macro. Loaded fresh per macro (load_macro()) --
    per UI_THEMING_GUIDE.md, there's no live-restyle broadcast and no
    live-reload either; Save writes to disk and restarts the daemon
    service, same as the old GTK editor."""

    def __init__(self, on_saved, parent=None):
        super().__init__(parent)
        self._on_saved = on_saved
        self._macro_id: str | None = None
        self._locked = False

        theme = Theme()

        self.name_edit = CustomLineEdit()
        self.content_layout.addWidget(self.name_edit)

        options_box = CustomGroupBox("Options")
        options_layout = options_box.make_layout(QVBoxLayout)

        row1 = QHBoxLayout()
        self.repeat_mode_combo = QComboBox()
        self.repeat_mode_combo.addItems(REPEAT_MODES)
        self.repeat_mode_combo.setStyleSheet(combo_box_stylesheet(theme_config.get_settings()))
        row1.addWidget(QLabel("Repeat mode:"))
        row1.addWidget(self.repeat_mode_combo)

        self.trigger_edge_combo = QComboBox()
        self.trigger_edge_combo.addItems(TRIGGER_EDGES)
        self.trigger_edge_combo.setStyleSheet(combo_box_stylesheet(theme_config.get_settings()))
        row1.addWidget(QLabel("Trigger edge:"))
        row1.addWidget(self.trigger_edge_combo)
        options_layout.addLayout(row1)

        self.ignore_keyboard_cb = CustomCheckBox("Ignore keyboard")
        self.ignore_mouse_buttons_cb = CustomCheckBox("Ignore mouse buttons")
        self.ignore_mouse_movement_cb = CustomCheckBox("Ignore mouse movement")
        self.simplified_names_cb = CustomCheckBox("Simplified names")
        self.python_on_cb = CustomCheckBox("Run as embedded Python (uncheck for the fast native path -- primitives only, no control flow)")
        for cb in (self.ignore_keyboard_cb, self.ignore_mouse_buttons_cb, self.ignore_mouse_movement_cb,
                   self.simplified_names_cb, self.python_on_cb):
            options_layout.addWidget(cb)

        self.content_layout.addWidget(options_box)

        combo_row = QHBoxLayout()
        self.combo_label = QLabel("Combo: (none)")
        self.combo_label.setStyleSheet(f"QLabel {{ color: {theme.text().name()}; }}")
        self.record_btn = CustomButton("Record Combo")
        self.record_btn.clicked.connect(self._toggle_recording)
        self.record_time_spin = CustomDoubleSpinBox()
        self.record_time_spin.setRange(0.5, 10.0)
        self.record_time_spin.setSingleStep(0.5)
        self.record_time_spin.setValue(3.0)
        combo_row.addWidget(self.combo_label)
        combo_row.addWidget(self.record_btn)
        combo_row.addWidget(QLabel("Record time (s):"))
        combo_row.addWidget(self.record_time_spin)
        self.content_layout.addLayout(combo_row)

        self.code_edit = ZoomablePlainTextEdit()
        self.content_layout.addWidget(self.code_edit, stretch=1)

        button_row = QHBoxLayout()
        self.lock_toggle = LockToggle()
        self.lock_toggle.toggled.connect(self._apply_lock_state)
        save_btn = CustomButton("Save")
        save_btn.clicked.connect(self._save)
        button_row.addWidget(self.lock_toggle)
        button_row.addStretch(1)
        button_row.addWidget(save_btn)
        self.content_layout.addLayout(button_row)

        self._recorder: ComboRecorder | None = None
        self._recorded_combo: list[str] = []

    def _apply_lock_state(self, locked: bool) -> None:
        self._locked = locked
        for widget in (self.record_btn, self.trigger_edge_combo):
            widget.setEnabled(not locked)

    def load_macro(self, macro_id: str | None) -> None:
        self._macro_id = macro_id
        macros_doc = cfg.load_macros()
        macro_def = next((m for m in macros_doc.get("macros", []) if m.get("id") == macro_id), None)
        if macro_def is None:
            macro_def = {"id": None, "name": "New Macro", "code": "", "combo": [],
                          "repeat_mode": "none", "trigger_edge": "down", "python_on": True}

        self.name_edit.setText(macro_def.get("name", ""))
        self.code_edit.setPlainText(macro_def.get("code", ""))
        self.repeat_mode_combo.setCurrentText(macro_def.get("repeat_mode", "none"))
        self.trigger_edge_combo.setCurrentText(macro_def.get("trigger_edge", "down"))
        self.ignore_keyboard_cb.setChecked(macro_def.get("ignore_keyboard", False))
        self.ignore_mouse_buttons_cb.setChecked(macro_def.get("ignore_mouse_buttons", False))
        self.ignore_mouse_movement_cb.setChecked(macro_def.get("ignore_mouse_movement", False))
        self.simplified_names_cb.setChecked(macro_def.get("simplified_names", False))
        self.python_on_cb.setChecked(macro_def.get("python_on", True))
        self._recorded_combo = list(macro_def.get("combo", []))
        self.combo_label.setText("Combo: " + (" + ".join(self._recorded_combo) or "(none)"))
        self.lock_toggle.setChecked(macro_def.get("locked", False))
        self._apply_lock_state(self.lock_toggle.isChecked())

    def _toggle_recording(self) -> None:
        if self._recorder is not None:
            return
        state = cfg.load_state()
        kb_path, mouse_path = state.get("keyboard_path"), state.get("mouse_path")
        if not kb_path or not mouse_path:
            QMessageBox.warning(self, "Puppetry", "No keyboard/mouse device configured yet.")
            return

        self._recorder = ComboRecorder(kb_path, mouse_path, self.record_time_spin.value())
        self.record_btn.setText("Recording... (release all keys)")
        self.record_btn.setEnabled(False)
        self._recorder.finished.connect(self._recording_finished)
        self._recorder.start()

    def _recording_finished(self, combo_names: list[str]) -> None:
        self.record_btn.setText("Record Combo")
        self.record_btn.setEnabled(True)
        self._recorder = None
        if combo_names:
            self._recorded_combo = combo_names
            self.combo_label.setText("Combo: " + " + ".join(combo_names))

    def _save(self) -> None:
        macros_doc = cfg.load_macros()
        macros = macros_doc.get("macros", [])
        macro_def = next((m for m in macros if m.get("id") == self._macro_id), None)
        if macro_def is None:
            import uuid
            macro_def = {"id": str(uuid.uuid4())}
            macros.append(macro_def)
            self._macro_id = macro_def["id"]

        macro_def["name"] = self.name_edit.text()
        macro_def["code"] = self.code_edit.toPlainText()
        macro_def["combo"] = self._recorded_combo
        macro_def["repeat_mode"] = self.repeat_mode_combo.currentText()
        macro_def["trigger_edge"] = self.trigger_edge_combo.currentText()
        macro_def["ignore_keyboard"] = self.ignore_keyboard_cb.isChecked()
        macro_def["ignore_mouse_buttons"] = self.ignore_mouse_buttons_cb.isChecked()
        macro_def["ignore_mouse_movement"] = self.ignore_mouse_movement_cb.isChecked()
        macro_def["simplified_names"] = self.simplified_names_cb.isChecked()
        macro_def["python_on"] = self.python_on_cb.isChecked()
        macro_def["locked"] = self.lock_toggle.isChecked()

        macros_doc["macros"] = macros
        cfg.save_macros(macros_doc)
        cfg.restart_daemon_service()
        self._on_saved()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Puppetry")
        theme = Theme()
        apply_window_palette(self, theme)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(140)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(theme.padding, theme.padding, theme.padding, theme.padding)
        sidebar_layout.setSpacing(theme.padding)

        self.stack = QStackedWidget()

        self.macro_list_page = MacroListPage(self._open_editor, self._new_macro)
        self.macro_editor_page = MacroEditorPage(self._back_to_list)
        self.stack.addWidget(self.macro_list_page)
        self.stack.addWidget(self.macro_editor_page)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        macros_btn = SegmentButton(text="Macros", position="full")
        macros_btn.setChecked(True)
        self.nav_group.addButton(macros_btn, 0)
        sidebar_layout.addWidget(macros_btn)
        sidebar_layout.addStretch(1)
        self.nav_group.idClicked.connect(lambda i: crossfade_to_index(self.stack, 0))

        root.addWidget(sidebar)
        root.addWidget(self.stack, stretch=1)

        self.resize(900, 600)

    def _open_editor(self, macro_id: str | None) -> None:
        self.macro_editor_page.load_macro(macro_id)
        crossfade_to_index(self.stack, 1)

    def _new_macro(self) -> None:
        self._open_editor(None)

    def _back_to_list(self) -> None:
        self.macro_list_page.refresh()
        crossfade_to_index(self.stack, 0)


def run_gui() -> int:
    cfg.ensure_config_exists()
    install_theme_provider()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


def run_cli(argv: list[str]) -> int:
    """`puppetry --name="Macro Name" [arg1 arg2 ...]` / `puppetry --abort`
    -- same contract the old GTK app's CLI mode offered, now just a
    plain socket+JSON client against the C++ daemon's control socket."""
    if argv[0] == "--abort":
        ok, msg = cfg.send_control_command({"cmd": "ABORT"})
    elif argv[0].startswith("--name="):
        name = argv[0][len("--name="):]
        ok, msg = cfg.send_control_command({"cmd": "FIRE", "name": name, "args": argv[1:]})
    else:
        print(f"puppetry: unrecognized argument {argv[0]!r}", file=sys.stderr)
        return 2
    if not ok:
        print(f"puppetry: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and (sys.argv[1] == "--abort" or sys.argv[1].startswith("--name=")):
        sys.exit(run_cli(sys.argv[1:]))
    sys.exit(run_gui())
