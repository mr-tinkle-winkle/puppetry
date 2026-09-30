"""
Macros page. Each row:
  [name box] ........ [combo] [x] [On Press/Release] [Repeat] [enabled] [Edit] [Delete] [lock]
- click the name box to rename inline;
- click the combo to record a new one (click again to cancel), x clears it;
- the enabled switch is per-profile (click "Profile: ..." top-left to switch);
- rows are grouped by category; a category's own switch turns every macro
  in it off (global, not per profile). Right-click a row to move it.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget

from input_tools import ComboRecorder
from model import AppModel
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.collapse_toggle_button import CollapseToggleButton
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.theme import Theme
from widgets import (
    LockToggle, NameBox, PageBase, ToggleSwitch, ask, label_style, mark_disable, mark_input, prompt_text,
    switch_profile_interactive,
)

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
        mark_disable(self.clear_btn)
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
        mark_disable(self.delete_btn)
        self.delete_btn.clicked.connect(self._delete)
        lay.addWidget(self.delete_btn)

        self.lock = LockToggle(bool(macro.get("locked", False)))
        self.lock.toggled.connect(self._lock_toggled)
        lay.addWidget(self.lock)
        self._apply_lock(self.lock.isChecked())
        self.sync_category_state()

    def sync_category_state(self) -> None:
        """Dim the row while its category is switched off."""
        cat = (self.model.find(self.macro_id) or {}).get("category") or ""
        on = self.model.category_enabled(cat)
        if on:
            self.setGraphicsEffect(None)
            self.enabled.setToolTip("Enabled in the current profile")
        else:
            eff = QGraphicsOpacityEffect(self)
            eff.setOpacity(0.45)
            self.setGraphicsEffect(eff)
            self.enabled.setToolTip(f"The \"{cat}\" category is switched off, so this macro is off too "
                                    "(this switch is remembered for when it's back on).")

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        cur = (self.model.find(self.macro_id) or {}).get("category") or ""
        sub = menu.addMenu("Move to category")
        for name in [""] + self.model.category_names():
            act = sub.addAction(name or "Uncategorized", lambda n=name: self.model.set_macro_category(self.macro_id, n))
            act.setCheckable(True)
            act.setChecked(name == cur)
        sub.addSeparator()
        sub.addAction("New category…", self._move_to_new_category)
        menu.exec(event.globalPos())

    def _move_to_new_category(self) -> None:
        name = prompt_text(self, "New category", "")
        if name and name.strip():
            name = self.model.add_category(name)
            self.model.set_macro_category(self.macro_id, name)

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


class CategoryHeader(QWidget):
    """[v] Name (n)  ........  [on/off] [^][v] [x]  -- one per category."""

    def __init__(self, page: "MacroListPage", name: str, count: int):
        super().__init__()
        self.page = page
        self.model: AppModel = page.model
        self.name = name
        theme = Theme()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, theme.padding // 2, 0, 0)
        self.toggle = CollapseToggleButton(expanded=not self.model.is_collapsed(name))
        self.toggle.setToolTip("Show / hide this category's macros")
        self.toggle.toggled.connect(self._collapse_toggled)
        lay.addWidget(self.toggle)
        if name:
            self.title = NameBox(name)
            self.title.setToolTip("Click to rename the category")
            self.title.renamed.connect(lambda n: self.model.rename_category(self.name, n))
        else:
            self.title = QLabel("Uncategorized")
            self.title.setStyleSheet(label_style(theme.text(), "font-size: 15px; font-weight: bold;"))
        lay.addWidget(self.title)
        count_lbl = QLabel(f"{count} macro{'s' if count != 1 else ''}")
        count_lbl.setStyleSheet(label_style(theme.text().darker(140)))
        lay.addWidget(count_lbl)
        lay.addStretch(1)
        self.enabled = None
        if name:
            self.enabled = ToggleSwitch(self.model.category_enabled(name))
            self.enabled.setToolTip("Switch every macro in this category on/off (in every profile -- each "
                                    "macro's own switch is kept for when the category is back on)")
            self.enabled.toggled.connect(lambda on: self.model.set_category_enabled(self.name, on))
            lay.addWidget(self.enabled)
            up = CustomButton("▲")
            up.setToolTip("Move category up")
            up.clicked.connect(lambda: self.model.move_category(self.name, -1))
            down = CustomButton("▼")
            down.setToolTip("Move category down")
            down.clicked.connect(lambda: self.model.move_category(self.name, 1))
            names = self.model.category_names()
            up.setEnabled(names.index(name) > 0)
            down.setEnabled(names.index(name) < len(names) - 1)
            remove = CustomButton("✕")
            remove.setToolTip("Delete this category (its macros move to Uncategorized)")
            mark_disable(remove)
            remove.clicked.connect(self._delete)
            for w in (up, down, remove):
                lay.addWidget(w)

    def _collapse_toggled(self, expanded: bool) -> None:
        self.model.set_collapsed(self.name, not expanded)
        self.page.apply_collapse(self.name, expanded)

    def _delete(self) -> None:
        if ask(self, f"Delete the category '{self.name}'?", "Its macros aren't deleted -- they move to Uncategorized.",
               ["Cancel", "Delete"]) == 1:
            self.model.delete_category(self.name)


class MacroListPage(PageBase):
    def __init__(self, model: AppModel, open_editor, parent=None):
        super().__init__(parent)
        self.model = model
        self.open_editor = open_editor
        theme = Theme()

        top = QHBoxLayout()
        self.profile_label = CustomButton("")
        self.profile_label.setToolTip("Click to switch profile")
        self.profile_label.clicked.connect(self._profile_menu)
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
        self.new_cat_btn = CustomButton("+ New Category")
        self.new_cat_btn.clicked.connect(self._new_category)
        bottom.addWidget(self.new_cat_btn)
        self.new_btn = CustomButton("+ New Macro")
        self.new_btn.clicked.connect(lambda: open_editor(None))
        bottom.addWidget(self.new_btn)
        self.outer_layout.addLayout(bottom)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast.setText(""))

        model.macros_changed.connect(self.refresh)
        model.categories_toggled.connect(self._category_toggled)
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
        self.profile_label.setText(f"Profile: {self.model.profile.get('name', self.model.profile_id)}  ▾")

    def _profile_menu(self) -> None:
        menu = QMenu(self)
        for pid, name in self.model.ordered_profiles():
            act = menu.addAction(name, lambda p=pid: switch_profile_interactive(self, self.model, p))
            act.setCheckable(True)
            act.setChecked(pid == self.model.profile_id)
        menu.exec(self.profile_label.mapToGlobal(self.profile_label.rect().bottomLeft()))

    def _new_category(self) -> None:
        name = prompt_text(self, "New category", "")
        if name is not None and name.strip():
            self.model.add_category(name)

    def _category_toggled(self, _name: str) -> None:
        for row in self.rows:
            row.sync_category_state()

    def apply_collapse(self, name: str, expanded: bool) -> None:
        for row in self.rows:
            if ((self.model.find(row.macro_id) or {}).get("category") or "") == name:
                row.setVisible(expanded)

    def refresh(self) -> None:
        # Rows are rebuilt only when the list's structure changes (add,
        # delete, editor save, profile switch) -- field edits made on a
        # row don't round-trip through here (pitfall #14: no redundant
        # rebuilds on click).
        for row in self.rows:
            row.stop_threads()
        while self.rows_box.count():
            w = self.rows_box.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.rows = []
        self.headers: dict[str, CategoryHeader] = {}
        groups: dict[str, list] = {"": []}
        for name in self.model.category_names():
            groups[name] = []
        for macro in self.model.macros():
            groups.setdefault(macro.get("category") or "", []).append(macro)
        show_headers = len(groups) > 1
        for name, macros in groups.items():
            if name == "" and not macros and show_headers:
                continue
            if show_headers:
                header = CategoryHeader(self, name, len(macros))
                self.headers[name] = header
                self.rows_box.addWidget(header)
            expanded = not self.model.is_collapsed(name) or not show_headers
            for macro in macros:
                row = MacroRow(self, macro)
                row.setVisible(expanded)
                self.rows_box.addWidget(row)
                self.rows.append(row)
        self._update_profile_label()
