"""Two sections of the macro editor's settings column:

PauseConditionsEditor -- the macro's "pause_conditions" list (macros.json):
    {"kind": "keys", "keys": [...], "mode": "toggle"|"while"}
    {"kind": "app", "match": "class"|"title", "value": "...",
     "state": "unfocused"|"focused", "mode": "toggle"|"while"}
  Saved with the macro (part of the macro dict, so the editor's unsaved-
  changes check sees it). The daemon does the pausing: native/src/pause.*.

SavedVariablesEditor -- this macro's set in saved_variables.json
  ({"<macro id>": {"<name>": value}}), the store behind saveVar/loadVar.
  Edits are written straight away (not with the macro), and the table
  re-reads the file while it's visible so values a running macro saves
  show up live.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import puppetry_config as cfg
from ui_kit.custom_button import CustomButton
from ui_kit.custom_line_edit import CustomLineEdit
from widgets import dim_label, mark_disable, mark_enable

SAVED_VARIABLES_FILE = cfg.CONFIG_DIR / "saved_variables.json"

MODES = ["toggle", "while"]
MODE_LABELS = ["Toggle", "While"]
APP_STATES = ["unfocused", "focused"]
APP_STATE_LABELS = ["is not focused", "is focused"]
MATCHES = ["class", "title"]
MATCH_LABELS = ["Window class", "Window title"]


def clean_conditions(conds) -> list:
    """Drops incomplete rows (no keys / no app) so they never reach the daemon."""
    out = []
    for c in conds or []:
        if not isinstance(c, dict):
            continue
        mode = c.get("mode") if c.get("mode") in MODES else "while"
        if c.get("kind") == "app":
            value = str(c.get("value", "")).strip()
            if value:
                out.append({"kind": "app", "match": c.get("match") if c.get("match") in MATCHES else "class",
                            "value": value,
                            "state": c.get("state") if c.get("state") in APP_STATES else "unfocused",
                            "mode": mode})
        else:
            keys = [k for k in c.get("keys", []) if isinstance(k, str) and k]
            if keys:
                out.append({"kind": "keys", "keys": keys, "mode": mode})
    return out


def describe_condition(c: dict) -> str:
    if c.get("kind") == "app":
        what = f"{'title' if c.get('match') == 'title' else 'app'} “{c.get('value', '')}” " \
               f"{'is focused' if c.get('state') == 'focused' else 'is not focused'}"
        return f"while {what}" if c.get("mode") == "while" else f"each time {what} (toggle)"
    keys = " + ".join(c.get("keys", []))
    return f"while {keys} is held" if c.get("mode") == "while" else f"{keys} toggles pause"


class _ConditionRow(QWidget):
    changed = Signal()
    removed = Signal(object)

    def __init__(self, cond: dict, css: str, record_keys, parent=None):
        super().__init__(parent)
        self._record_keys = record_keys          # callable(row) -> starts a combo recording for this row
        self.keys: list = list(cond.get("keys", []))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.kind = QComboBox()
        self.kind.addItems(["Keys", "App"])
        self.kind.setStyleSheet(css)
        self.kind.setToolTip("Keys: a key or key combo. App: a window being focused or not.")
        lay.addWidget(self.kind)

        # keys
        self.keys_label = QLabel()
        self.keys_label.setFont(QFont("monospace"))
        self.keys_label.setMinimumWidth(10)
        self.keys_label.setWordWrap(True)
        self.record_btn = CustomButton("Record")
        self.record_btn.clicked.connect(lambda: self._record_keys(self))
        # app
        self.match = QComboBox()
        self.match.addItems(MATCH_LABELS)
        self.match.setStyleSheet(css)
        self.match.setToolTip("Window class: the app's id (e.g. sober, firefox). Window title: text in its title bar. "
                              "Either is matched as a case-insensitive part of the name.")
        self.value = CustomLineEdit()
        self.value.setPlaceholderText("e.g. sober")
        self.state = QComboBox()
        self.state.addItems(APP_STATE_LABELS)
        self.state.setStyleSheet(css)
        for w in (self.keys_label, self.record_btn, self.match, self.value, self.state):
            lay.addWidget(w, stretch=1 if w in (self.keys_label, self.value) else 0)

        self.mode = QComboBox()
        self.mode.addItems(MODE_LABELS)
        self.mode.setStyleSheet(css)
        self.mode.setToolTip("Toggle: each time it happens, the macro switches between paused and running.\n"
                             "While: the macro is paused for as long as it holds.")
        lay.addWidget(self.mode)
        rm = CustomButton("✕")
        rm.setToolTip("Remove this condition")
        mark_disable(rm)
        rm.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(rm)

        self.kind.setCurrentIndex(1 if cond.get("kind") == "app" else 0)
        self.match.setCurrentIndex(1 if cond.get("match") == "title" else 0)
        self.value.setText(str(cond.get("value", "")))
        self.state.setCurrentIndex(1 if cond.get("state") == "focused" else 0)
        self.mode.setCurrentIndex(0 if cond.get("mode") == "toggle" else 1)
        self._update_keys_label()
        self._show_kind()
        self.kind.currentIndexChanged.connect(lambda _i: (self._show_kind(), self.changed.emit()))
        for w in (self.match, self.state, self.mode):
            w.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.value.textChanged.connect(lambda _t: self.changed.emit())

    def _show_kind(self) -> None:
        app = self.kind.currentIndex() == 1
        for w in (self.keys_label, self.record_btn):
            w.setVisible(not app)
        for w in (self.match, self.value, self.state):
            w.setVisible(app)

    def _update_keys_label(self) -> None:
        self.keys_label.setText(" + ".join(self.keys) if self.keys else "(press Record)")

    def set_keys(self, names: list) -> None:
        self.keys = list(names)
        self._update_keys_label()
        self.changed.emit()

    def set_recording(self, on: bool, text: str = "") -> None:
        self.record_btn.setText("Cancel" if on else "Record")
        if on:
            self.keys_label.setText(text or "(listening…)")
        else:
            self._update_keys_label()

    def value_dict(self) -> dict:
        mode = MODES[self.mode.currentIndex()]
        if self.kind.currentIndex() == 1:
            return {"kind": "app", "match": MATCHES[self.match.currentIndex()], "value": self.value.text().strip(),
                    "state": APP_STATES[self.state.currentIndex()], "mode": mode}
        return {"kind": "keys", "keys": list(self.keys), "mode": mode}


class PauseConditionsEditor(QWidget):
    """Rows of conditions + an Add button. `start_recording(row)` is provided
    by the editor page (it owns the ComboRecorder)."""

    changed = Signal()

    def __init__(self, css: str, start_recording, parent=None):
        super().__init__(parent)
        self._css = css
        self._start_recording = start_recording
        self.rows: list = []
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        self._rows_box = QVBoxLayout()
        col.addLayout(self._rows_box)
        add_row = QHBoxLayout()
        add = CustomButton("Add condition")
        mark_enable(add)
        add.clicked.connect(lambda: self._add({"kind": "keys", "keys": [], "mode": "toggle"}, emit=True))
        add_row.addWidget(add)
        add_row.addStretch(1)
        col.addLayout(add_row)
        col.addWidget(dim_label(
            "Pauses this macro whenever any condition says so. Paused: a running macro waits where it is and "
            "carries on when it resumes, its key combo starts nothing (a looping toggle macro can still be "
            "switched off), keys it remapped with actAs() act as themselves, and getMouseMotion() in it leaves "
            "out what the mouse did meanwhile. App conditions need kdotool (KDE Plasma)."))

    def set_conditions(self, conds) -> None:
        for r in list(self.rows):
            self._remove(r, emit=False)
        for c in conds or []:
            if isinstance(c, dict):
                self._add(c, emit=False)

    def conditions(self) -> list:
        return clean_conditions([r.value_dict() for r in self.rows])

    def _add(self, cond: dict, emit: bool) -> None:
        row = _ConditionRow(cond, self._css, self._start_recording)
        row.changed.connect(self.changed.emit)
        row.removed.connect(lambda r: self._remove(r, emit=True))
        self.rows.append(row)
        self._rows_box.addWidget(row)
        if emit:
            self.changed.emit()

    def _remove(self, row, emit: bool) -> None:
        if row in self.rows:
            self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        if emit:
            self.changed.emit()


# ---------------------------------------------------------------------------
# saved variables
# ---------------------------------------------------------------------------

def load_saved_variables(path: Path = None) -> dict:
    path = path or SAVED_VARIABLES_FILE
    try:
        doc = json.loads(Path(path).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_saved_variables(doc: dict, path: Path = None) -> None:
    path = Path(path or SAVED_VARIABLES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)


def parse_value(text: str):
    """What's typed in the Value box -> the stored value. JSON first (1, 2.5,
    "x", true, [1, 2]), then Python literals (True, None, 'x', (1, 2)), else
    the text itself as a string."""
    t = text.strip()
    if t == "":
        return ""
    try:
        return json.loads(t)
    except ValueError:
        pass
    try:
        v = ast.literal_eval(t)
        json.dumps(v)            # only things the daemon can store
        return list(v) if isinstance(v, tuple) else v
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return text


def format_value(v) -> str:
    """Stored value -> how macro code would write it."""
    if v is True:
        return "True"
    if v is False:
        return "False"
    if v is None:
        return "None"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, dict)):
        return repr(_pyish(v))
    return repr(v)


def _pyish(v):
    if isinstance(v, list):
        return [_pyish(x) for x in v]
    if isinstance(v, dict):
        return {k: _pyish(x) for k, x in v.items()}
    return v


def set_variable(owner: str, name: str, value, path: Path = None) -> None:
    doc = load_saved_variables(path)
    vars_ = doc.get(owner) if isinstance(doc.get(owner), dict) else {}
    vars_[name] = value
    doc[owner] = vars_
    save_saved_variables(doc, path)


def delete_variable(owner: str, name: str, path: Path = None) -> None:
    doc = load_saved_variables(path)
    vars_ = doc.get(owner)
    if isinstance(vars_, dict) and name in vars_:
        del vars_[name]
        if not vars_:
            del doc[owner]
        save_saved_variables(doc, path)


class _VarRow(QWidget):
    def __init__(self, owner_editor, name: str, value, parent=None):
        super().__init__(parent)
        self.ed = owner_editor
        self.name = name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.name_edit = CustomLineEdit()
        self.name_edit.setText(name)
        self.name_edit.setPlaceholderText("name")
        self.value_edit = CustomLineEdit()
        self.value_edit.setText("" if name == "" else format_value(value))
        self.value_edit.setPlaceholderText("value, e.g. 2400 or \"text\" or [1, 2]")
        self.value_edit.setFont(QFont("monospace"))
        lay.addWidget(self.name_edit, stretch=1)
        lay.addWidget(QLabel("="))
        lay.addWidget(self.value_edit, stretch=2)
        rm = CustomButton("✕")
        rm.setToolTip("Forget this variable")
        mark_disable(rm)
        rm.clicked.connect(lambda: self.ed._delete_row(self))
        lay.addWidget(rm)
        self.name_edit.editingFinished.connect(lambda: self.ed._commit_row(self))
        self.value_edit.editingFinished.connect(lambda: self.ed._commit_row(self))

    def editing(self) -> bool:
        return self.name_edit.hasFocus() or self.value_edit.hasFocus()


class SavedVariablesEditor(QWidget):
    def __init__(self, path: Path = None, parent=None):
        super().__init__(parent)
        self.path = path
        self.owner: str | None = None
        self.rows: list = []
        self._shown: dict = {}
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        self.note = dim_label("")
        col.addWidget(self.note)
        self._rows_box = QVBoxLayout()
        col.addLayout(self._rows_box)
        add_row = QHBoxLayout()
        self.add_btn = CustomButton("Add variable")
        mark_enable(self.add_btn)
        self.add_btn.clicked.connect(self._add_blank)
        add_row.addWidget(self.add_btn)
        add_row.addStretch(1)
        col.addLayout(add_row)
        col.addWidget(dim_label(
            "Values this macro keeps with saveVar(\"name\", value) and reads with loadVar(\"name\", default) -- "
            "kept across runs, restarts and reboots. Set one here by hand, or let the macro set it. Changes here "
            "are saved right away (not with the macro); values the macro saves show up while this is open."))
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)

    def set_owner(self, macro_id: str | None) -> None:
        self.owner = macro_id
        self._shown = {}
        self.refresh(force=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _current(self) -> dict:
        if not self.owner:
            return {}
        v = load_saved_variables(self.path).get(self.owner)
        return v if isinstance(v, dict) else {}

    def refresh(self, force: bool = False) -> None:
        self.add_btn.setEnabled(bool(self.owner))
        if not self.owner:
            self.note.setText("Save the macro once to give it saved variables.")
            self._rebuild({})
            return
        cur = self._current()
        self.note.setText("" if cur else "None yet.")
        if not force and (cur == self._shown or any(r.editing() for r in self.rows)):
            return
        self._rebuild(cur)

    def _rebuild(self, cur: dict) -> None:
        blanks = [r for r in self.rows if r.name == "" and r.name_edit.text().strip() == ""]
        for r in list(self.rows):
            if r in blanks:
                continue
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        for name in sorted(cur):
            row = _VarRow(self, name, cur[name])
            self.rows.append(row)
            self._rows_box.addWidget(row)
        for r in blanks:            # keep a just-added empty row
            self.rows.append(r)
            self._rows_box.addWidget(r)
        self._shown = dict(cur)

    def _add_blank(self) -> None:
        if not self.owner:
            return
        row = _VarRow(self, "", None)
        self.rows.append(row)
        self._rows_box.addWidget(row)
        row.name_edit.setFocus()

    def _commit_row(self, row: _VarRow) -> None:
        if not self.owner:
            return
        name = row.name_edit.text().strip()
        if not name:
            return
        value = parse_value(row.value_edit.text())
        if row.name and row.name != name:
            delete_variable(self.owner, row.name, self.path)
        set_variable(self.owner, name, value, self.path)
        row.name = name
        self._shown = self._current()

    def _delete_row(self, row: _VarRow) -> None:
        if self.owner and row.name:
            delete_variable(self.owner, row.name, self.path)
        if row in self.rows:
            self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._shown = self._current()
        self.note.setText("" if self._shown else "None yet.")
