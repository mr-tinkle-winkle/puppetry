"""
Macros page. Each row:
  [name box] ........ [combo] [x] [On Press/Release] [Repeat] [enabled] [Edit] [Delete] [lock]
- click the name box to rename inline;
- click the combo to record a new one (click again to cancel), x clears it;
- the enabled switch is per-profile (Settings -> Profiles picks which).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from input_tools import ComboRecorder
from model import AppModel
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.theme import Theme
from widgets import LockToggle, NameBox, PageBase, ToggleSwitch, ask, label_style, mark_input

REPEAT_MODES = ["none", "hold", "toggle"]
REPEAT_LABELS = ["No Repeat", "Hold", "Toggle"]
EDGES = ["down", "up"]
EDGE_LABELS = ["On Press", "On Release"]


def combo_text(names: list[str]) -> str:
    return " + ".join(names) if names else "(no combo)"


class MacroRow(QWidget):
    def __init__(self, page: "MacroListPage", macro: dict):
        super().__init__()
        self.page = page
        self.model: AppModel = page.model
        self.macro_id = macro["id"]
        self._recorder: ComboRecorder | None = None
        combo_css = combo_box_stylesheet(theme_config.get_settings())

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(Theme().padding // 2 + 2)

        self.name_box = NameBox(macro.get("name", "(unnamed)"))
        self.name_box.renamed.connect(lambda n: self.model.set_field(self.macro_id, "name", n))
        lay.addWidget(self.name_box)
        lay.addStretch(1)

        self.combo_btn = CustomButton(combo_text(macro.get("combo", [])))
        mark_input(self.combo_btn)   # the trigger: real input
        self.combo_btn.setToolTip("Click, then hold your combo still to rebind it (click again to cancel)")
        self.combo_btn.clicked.connect(self._combo_clicked)
        lay.addWidget(self.combo_btn)

        self.clear_btn = CustomButton("✕")
        self.clear_btn.setToolTip("Remove this macro's combo")
        self.clear_btn.clicked.connect(self._clear_combo)
        lay.addWidget(self.clear_btn)

        self.edge = QComboBox()
        self.edge.addItems(EDGE_LABELS)
        self.edge.setStyleSheet(combo_css)
        self.edge.setCurrentIndex(EDGES.index(macro.get("trigger_edge", "down")) if macro.get("trigger_edge") in EDGES else 0)
        self.edge.setToolTip("On Press: fires the moment the combo is fully held.\n"
                             "On Release: fires once every combo key has been let go.")
        self.edge.currentIndexChanged.connect(lambda i: self.model.set_field(self.macro_id, "trigger_edge", EDGES[i]))
        lay.addWidget(self.edge)

        self.repeat = QComboBox()
        self.repeat.addItems(REPEAT_LABELS)
        self.repeat.setStyleSheet(combo_css)
        mode = macro.get("repeat_mode", "none")
        self.repeat.setCurrentIndex(REPEAT_MODES.index(mode) if mode in REPEAT_MODES else 0)
        self.repeat.currentIndexChanged.connect(lambda i: self.model.set_field(self.macro_id, "repeat_mode", REPEAT_MODES[i]))
        lay.addWidget(self.repeat)

        self.enabled = ToggleSwitch(self.model.is_enabled(self.macro_id))
        self.enabled.setToolTip("Enabled in the current profile")
        self.enabled.toggled.connect(lambda on: self.model.set_enabled(self.macro_id, on))
        lay.addWidget(self.enabled)

        self.edit_btn = CustomButton("Edit")
        self.edit_btn.clicked.connect(lambda: page.open_editor(self.macro_id))
        lay.addWidget(self.edit_btn)

        self.delete_btn = CustomButton("Delete")
        self.delete_btn.clicked.connect(self._delete)
        lay.addWidget(self.delete_btn)

        self.lock = LockToggle(bool(macro.get("locked", False)))
        self.lock.toggled.connect(self._lock_toggled)
        lay.addWidget(self.lock)
        self._apply_lock(self.lock.isChecked())

    # -- lock
    def _apply_lock(self, locked: bool) -> None:
        for w in (self.name_box, self.combo_btn, self.clear_btn, self.edge, self.edit_btn, self.delete_btn):
            w.setEnabled(not locked)

    def _lock_toggled(self, locked: bool) -> None:
        self._apply_lock(locked)
        self.model.set_field(self.macro_id, "locked", locked)

    # -- combo
    def _combo_clicked(self) -> None:
        if self._recorder is not None:
            self._recorder.cancel()
            return
        kb, _ = self.model.device("keyboard")
        mouse, _ = self.model.device("mouse")
        if not kb and not mouse:
            self.combo_btn.setText("Set devices in Settings first")
            return
        self._recorder = ComboRecorder(kb, mouse, float(self.model.state.get("record_time_seconds", 3.0)), self)
        self._recorder.progress.connect(self._combo_progress)
        self._recorder.finished_combo.connect(self._combo_done)
        self.combo_btn.setText("Recording… (click to cancel)")
        self._recorder.start()

    def _combo_progress(self, names: list, left: float) -> None:
        if not names:
            self.combo_btn.setText("(listening…)")
        elif left > 0:
            self.combo_btn.setText(f"{combo_text(names)} ({left:.1f}s)")
        else:
            self.combo_btn.setText(f"{combo_text(names)} ✓")

    def _combo_done(self, names: list, status: str) -> None:
        rec, self._recorder = self._recorder, None
        if rec is not None:
            rec.wait(500)
        macro = self.model.find(self.macro_id) or {}
        if status == "ok":
            self.combo_btn.setText(combo_text(names))
            self.model.set_field(self.macro_id, "combo", names)
        elif status == "no_devices":
            self.combo_btn.setText("Device error")
        else:
            self.combo_btn.setText(combo_text(macro.get("combo", [])))

    def _clear_combo(self) -> None:
        if self._recorder is not None:
            return
        self.combo_btn.setText(combo_text([]))
        self.model.set_field(self.macro_id, "combo", [])

    def _delete(self) -> None:
        name = (self.model.find(self.macro_id) or {}).get("name", "this macro")
        if ask(self, f"Delete '{name}'?", "This can't be undone.", ["Cancel", "Delete"]) == 1:
            self.model.delete_macro(self.macro_id)

    def stop_threads(self) -> None:
        if self._recorder is not None:
            self._recorder.cancel()
            self._recorder.wait(1000)


class MacroListPage(PageBase):
    def __init__(self, model: AppModel, open_editor, parent=None):
        super().__init__(parent)
        self.model = model
        self.open_editor = open_editor
        theme = Theme()

        top = QHBoxLayout()
        self.profile_label = QLabel()
        self.profile_label.setStyleSheet(label_style(theme.text(), "font-weight: bold;"))
        top.addWidget(self.profile_label)
        self.status = QLabel("")
        self.status.setStyleSheet(label_style(theme.text().darker(130)))
        top.addWidget(self.status, stretch=1)
        self.save_btn = CustomButton("Save (restarts daemon)")
        self.save_btn.clicked.connect(model.save)
        top.addWidget(self.save_btn)
        self.content_layout.addLayout(top)

        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(theme.padding // 2 + 4)
        self.content_layout.addLayout(self.rows_box)
        self.content_layout.addStretch(1)
        self.rows: list[MacroRow] = []

        # Fixed bar below the scrolling list, bottom-right, like the
        # editor's own bottom bar -- stays in place instead of scrolling
        # away with the row list.
        bottom = QHBoxLayout()
        bottom.setContentsMargins(theme.padding, 0, theme.padding, theme.padding)
        self.toast = QLabel("")
        self.toast.setStyleSheet(label_style(theme.text()))
        bottom.addWidget(self.toast)
        bottom.addStretch(1)
        self.new_btn = CustomButton("+ New Macro")
        self.new_btn.clicked.connect(lambda: open_editor(None))
        bottom.addWidget(self.new_btn)
        self.outer_layout.addLayout(bottom)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast.setText(""))

        model.macros_changed.connect(self.refresh)
        model.profiles_changed.connect(self._update_profile_label)
        model.dirty_changed.connect(self._dirty)
        model.status.connect(self.status.setText)
        self._dirty(model.dirty)
        self._update_profile_label()
        self.refresh()

    def show_toast(self, message: str, ms: int = 2500) -> None:
        """A brief bottom-of-page notice, next to + New Macro -- e.g. the
        "Please select a macro" nudge from the sidebar's Macro Editor
        entry when nothing's open yet."""
        self.toast.setText(message)
        self._toast_timer.start(ms)

    def _dirty(self, dirty: bool) -> None:
        self.save_btn.setVisible(dirty and not self.model.state.get("autosave"))

    def _update_profile_label(self) -> None:
        self.profile_label.setText(f"Profile: {self.model.profile.get('name', self.model.profile_id)}")

    def refresh(self) -> None:
        # Rows are rebuilt only when the list's structure changes (add,
        # delete, editor save, profile switch) -- field edits made on a
        # row don't round-trip through here (pitfall #14: no redundant
        # rebuilds on click).
        for row in self.rows:
            row.stop_threads()
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        for macro in self.model.macros():
            row = MacroRow(self, macro)
            self.rows_box.addWidget(row)
            self.rows.append(row)
        self._update_profile_label()
