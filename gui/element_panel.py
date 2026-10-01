"""
ElementPanel: the sidebar for arranging the input visualizer's elements.

Shown beside the capture area in Edit layout, and at the top of the
overlay's Customize dialog. It lists every element of the scene; the
selected one gets its options as touching toggle rows:

  keyboard   Size: Full | 80% | 60% | Half
  mouse      Look: Classic | Gaming | Minimal | Buttons
  movement   Style: Comet | Mousepad | Joystick
             Comet follows: Head | Tail
             Invert side button rings
  any        Size (scale), Remove

The panel edits the scene dict it was given in place and then calls
on_change(); on_select(id) reports selection changes (so the capture area
can outline the same element).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import kbm_layout as kl
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_spinbox import CustomDoubleSpinBox
from ui_kit.segment_button import SegmentButton
from ui_kit.theme import Theme
from widgets import dim_label, label_style

PANEL_WIDTH = 340

KEYBOARD_SHORT = {"full": "Full", "tkl": "80%", "60": "60%", "half": "Half"}
MOUSE_SHORT = {"classic": "Classic", "gaming": "Gaming", "minimal": "Minimal", "buttons": "Buttons"}
CENTER_SHORT = {"head": "Head", "tail": "Tail"}


def element_title(el: dict) -> str:
    typ = el.get("type", "")
    name = kl.ELEMENT_TYPES.get(typ, typ)
    return name if el.get("id") == typ else f"{name} ({el.get('id')})"


class _Toggles(QWidget):
    """A row of touching SegmentButtons; exactly one is checked."""

    def __init__(self, options: dict, current, on_pick, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        keys = list(options)
        for i, k in enumerate(keys):
            pos = "full" if len(keys) == 1 else ("left" if i == 0 else "right" if i == len(keys) - 1 else "middle")
            b = SegmentButton(text=options[k], position=pos)
            b.setMinimumHeight(30)
            b.setMinimumWidth(10)
            b.setChecked(k == current)
            b.clicked.connect(lambda _c=False, k=k: on_pick(k))
            self.group.addButton(b)
            lay.addWidget(b, 1)
            self.buttons[k] = b

    def current(self):
        return next((k for k, b in self.buttons.items() if b.isChecked()), None)


class ElementPanel(QWidget):
    def __init__(self, scene: dict, on_change=None, on_select=None, fixed_width=True, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.on_change = on_change
        self.on_select = on_select
        self.selected = None
        if fixed_width:
            self.setFixedWidth(PANEL_WIDTH)
        else:
            self.setMinimumWidth(PANEL_WIDTH)
        theme = Theme()
        self._label_css = label_style(theme.text())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        head = QLabel("Elements")
        head.setStyleSheet(self._label_css + "font-weight: bold;")
        root.addWidget(head)
        # the element list: a touching vertical stack of toggle buttons, one per element
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(0)
        self.list_group = QButtonGroup(self)
        self.list_group.setExclusive(False)        # so "nothing selected" is possible
        self.list_buttons = {}
        root.addWidget(self.list_host)
        self.add_btn = CustomButton("+ Add element")
        self.add_btn.clicked.connect(self._add_menu)
        self.reset_btn = CustomButton("Reset layout")
        self.reset_btn.clicked.connect(self.reset)
        brow = QHBoxLayout()
        brow.addWidget(self.add_btn)
        brow.addWidget(self.reset_btn)
        brow.addStretch(1)
        root.addLayout(brow)
        self.opts_host = QWidget()
        self.opts = QVBoxLayout(self.opts_host)
        self.opts.setContentsMargins(0, 8, 0, 0)
        root.addWidget(self.opts_host)
        root.addStretch(1)
        self.widgets = {}            # name -> the option widgets currently shown (tests, refresh)
        self.refresh()

    # -- public -----------------------------------------------------------------
    def set_scene(self, scene: dict) -> None:
        self.scene = scene
        if self.selected and not self._el(self.selected):
            self.selected = None
        self.refresh()

    def select(self, el_id) -> None:
        """Selection from outside (the capture area)."""
        if el_id == self.selected:
            return
        self.selected = el_id
        self.refresh()

    def refresh(self) -> None:
        for b in list(self.list_buttons.values()):
            self.list_group.removeButton(b)
            b.setParent(None)
            b.deleteLater()
        self.list_buttons = {}
        els = self.scene.get("elements", [])
        for i, el in enumerate(els):
            pos = "full" if len(els) == 1 else ("top" if i == 0 else "bottom" if i == len(els) - 1 else "middle")
            b = SegmentButton(text=element_title(el), position=pos)
            b.setMinimumHeight(28)
            b.setChecked(el.get("id") == self.selected)
            b.clicked.connect(lambda _c=False, i_=el.get("id"): self._list_picked(i_))
            self.list_group.addButton(b)
            self.list_lay.addWidget(b)
            self.list_buttons[el.get("id")] = b
        self._build_options()

    def reset(self) -> None:
        import copy
        self.scene["elements"] = copy.deepcopy(kl.DEFAULT_SCENE["elements"])
        self.selected = None
        self._changed()

    def add(self, typ: str) -> dict:
        els = self.scene.setdefault("elements", [])
        right = max((kl.element_rect(e)[0] + kl.element_rect(e)[2] for e in els), default=-1.0)
        el = kl.default_element(self.scene, typ, right + 1.0, 0.0)
        els.append(el)
        self._pick(el["id"])
        self._changed()
        return el

    def remove(self, el_id) -> None:
        self.scene["elements"] = [e for e in self.scene.get("elements", []) if e.get("id") != el_id]
        if self.selected == el_id:
            self._pick(None)
        self._changed()

    # -- internals ----------------------------------------------------------------
    def _el(self, el_id):
        return next((e for e in self.scene.get("elements", []) if e.get("id") == el_id), None)

    def _pick(self, el_id) -> None:
        self.selected = el_id
        if self.on_select:
            self.on_select(el_id)

    def _list_picked(self, el_id) -> None:
        self._pick(None if el_id == self.selected else el_id)       # clicking the selected one deselects
        for k, b in self.list_buttons.items():
            b.setChecked(k == self.selected)
        self._build_options()

    def _changed(self) -> None:
        self.refresh()
        if self.on_change:
            self.on_change()

    def _set(self, key, value, rebuild: bool = True) -> None:
        """Change the selected element. rebuild=False for edits made from a
        widget that must survive the change (the scale box, a checkbox)."""
        el = self._el(self.selected)
        if el is None:
            return
        el[key] = value
        if key == "type" and value == "comet":
            el.setdefault("center", "head")
        if rebuild:
            self._changed()
        elif self.on_change:
            self.on_change()

    def _add_menu(self) -> None:
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        for typ, label in kl.ELEMENT_TYPES.items():
            menu.addAction(label, lambda t=typ: self.add(t))
        menu.exec(QCursor.pos())

    def _caption(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(self._label_css)
        lbl.setWordWrap(True)
        return lbl

    def _build_options(self) -> None:
        while self.opts.count():
            it = self.opts.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.widgets = {}
        el = self._el(self.selected)
        if el is None:
            self.opts.addWidget(dim_label("Select an element (here or in the picture) to change it."))
            return
        typ = el.get("type")
        title = QLabel(element_title(el))
        title.setStyleSheet(self._label_css + "font-weight: bold;")
        self.opts.addWidget(title)
        if typ == "keyboard":
            self.opts.addWidget(self._caption("Size"))
            self.widgets["layout"] = _Toggles(KEYBOARD_SHORT, el.get("layout", "tkl"), lambda k: self._set("layout", k))
            self.opts.addWidget(self.widgets["layout"])
            if el.get("layout", "tkl") == "60":
                cb = CustomCheckBox("Arrow keys (60% boards that have them)")
                cb.setChecked(bool(el.get("arrows", False)))
                cb.toggled.connect(lambda v: self._set("arrows", bool(v), rebuild=False))
                self.widgets["arrows"] = cb
                self.opts.addWidget(cb)
        elif typ == "mouse":
            self.opts.addWidget(self._caption("Look"))
            self.widgets["look"] = _Toggles(MOUSE_SHORT, el.get("look", "classic"), lambda k: self._set("look", k))
            self.opts.addWidget(self.widgets["look"])
        elif typ in kl.MOTION_TYPES:
            self.opts.addWidget(self._caption("Style"))
            self.widgets["type"] = _Toggles(kl.MOTION_TYPES, typ, lambda k: self._set("type", k))
            self.opts.addWidget(self.widgets["type"])
            if typ == "comet":
                self.opts.addWidget(self._caption("Comet follows"))
                self.widgets["center"] = _Toggles(CENTER_SHORT, el.get("center", "head"),
                                                  lambda k: self._set("center", k))
                self.opts.addWidget(self.widgets["center"])
            cb = CustomCheckBox("Invert side button rings")
            cb.setToolTip("Normally the back button's ring goes left and forward's goes right. On: swapped.")
            cb.setChecked(bool(el.get("invert_side", False)))
            cb.toggled.connect(lambda v: self._set("invert_side", bool(v), rebuild=False))
            self.widgets["invert_side"] = cb
            self.opts.addWidget(cb)
        row = QHBoxLayout()
        row.addWidget(self._caption("Scale"))
        sc = CustomDoubleSpinBox()
        sc.setRange(0.3, 4.0)
        sc.setSingleStep(0.05)
        sc.setDecimals(2)
        sc.setValue(float(el.get("scale", 1.0)))
        sc.valueChanged.connect(lambda v: self._set("scale", round(float(v), 2), rebuild=False))
        self.widgets["scale"] = sc
        row.addWidget(sc)
        row.addStretch(1)
        host = QWidget()
        host.setLayout(row)
        row.setContentsMargins(0, 4, 0, 0)
        self.opts.addWidget(host)
        rm = CustomButton("Remove")
        rm.clicked.connect(lambda: self.remove(el.get("id")))
        self.widgets["remove"] = rm
        rrow = QHBoxLayout()
        rrow.setContentsMargins(0, 0, 0, 0)
        rrow.addWidget(rm)
        rrow.addStretch(1)
        rhost = QWidget()
        rhost.setLayout(rrow)
        self.opts.addWidget(rhost)
