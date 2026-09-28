"""
"Create a custom block" dialog: name, palette category, color (orange /
blue / gray / purple presets or any hex), arguments (add/remove; each with
a default and where its dropdown/completions come from: any value,
keyboard keys, mouse buttons, keyboard + mouse, or a custom list), and the
code template ({name} is replaced by that argument). Saving validates the
generated code with the daemon's own compiler first.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget,
)

import custom_blocks
from block_model import CustomArg, CustomDef, custom_body_code
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.rounded_rect import rounded_rect_path
from ui_kit.theme import Theme, contrast_text
from widgets import dim_label, label_style, section_title


class _ArgRow(QWidget):
    def __init__(self, dialog: "CustomBlockDialog", arg: CustomArg):
        super().__init__()
        self.dialog = dialog
        css = combo_box_stylesheet(theme_config.get_settings())
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.name = CustomLineEdit(arg.name)
        self.name.setPlaceholderText("name")
        self.name.setToolTip("The argument's name -- write {name} in the code to use it.")
        self.default = CustomLineEdit(arg.default)
        self.default.setPlaceholderText("default")
        self.default.setToolTip("Used when the socket is left empty. Its type matters: 0 = a number, "
                                "KEY_A = a key, \"text\" = text, True/False.")
        self.source = QComboBox()
        self.source.setStyleSheet(css)
        for key, label in custom_blocks.ARG_SOURCES:
            self.source.addItem(label, key)
        idx = [k for k, _ in custom_blocks.ARG_SOURCES].index(arg.source) if arg.source in dict(custom_blocks.ARG_SOURCES) else 0
        self.source.setCurrentIndex(idx)
        self.source.setToolTip("Where the socket's choices come from (a dropdown or type-ahead suggestions).")
        self.options = CustomLineEdit(", ".join(arg.options))
        self.options.setPlaceholderText("option1, option2, ...")
        self.options.setToolTip("Custom list: the dropdown's choices, comma-separated (as code, e.g. \"fast\", 3).")
        remove = CustomButton("✕")
        remove.setToolTip("Remove this argument")
        remove.clicked.connect(lambda: dialog.remove_arg(self))
        lay.addWidget(self.name, 2)
        lay.addWidget(self.default, 2)
        lay.addWidget(self.source, 2)
        lay.addWidget(self.options, 3)
        lay.addWidget(remove)
        self.source.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        self.options.setVisible(self.source.currentData() == "list")

    def value(self) -> CustomArg:
        opts = [o.strip() for o in self.options.text().split(",") if o.strip()]
        return CustomArg(self.name.text().strip(), self.default.text().strip() or "0",
                         self.source.currentData() or "any", opts)


class CustomBlockDialog(QDialog):
    PRESETS = (("Orange (input)", "input"), ("Blue (output)", "output"), ("Gray (neutral)", "neutral"),
               ("Purple (custom)", "custom"))

    def __init__(self, parent, defn: "CustomDef | None", existing: dict, key_names: list):
        super().__init__(parent)
        self.setWindowTitle("Custom block")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._theme = Theme()
        self.result_def: CustomDef | None = None
        self._original = defn
        self._existing = existing
        d = defn or CustomDef(name="my block", category="My blocks", color=self._theme.custom_color().name(),
                              args=[CustomArg("x", "0")], template="move_mouse({x}, 0)\n")
        css = combo_box_stylesheet(theme_config.get_settings())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(section_title("Edit custom block" if defn else "Create a custom block"))
        lay.addWidget(dim_label("Custom blocks are shared by every macro. They run on the fast native path "
                                "unless you tick \"Run as embedded Python\"."))

        row = QHBoxLayout()
        row.addWidget(QLabel("Name"))
        self.name = CustomLineEdit(d.name)
        self.name.setToolTip("What the block says, e.g. \"click at\". Also becomes its code name (click_at).")
        row.addWidget(self.name, 1)
        row.addWidget(QLabel("Category"))
        self.category = QComboBox()
        self.category.setStyleSheet(css)
        self.category.addItems(custom_blocks.CATEGORIES)
        if d.category in custom_blocks.CATEGORIES:
            self.category.setCurrentText(d.category)
        self.category.setToolTip("Which palette section it shows up in.")
        row.addWidget(self.category)
        lay.addLayout(row)

        crow = QHBoxLayout()
        crow.addWidget(QLabel("Color"))
        for label, role in self.PRESETS:
            c = self._theme.category_color(role)
            b = CustomButton(label.split(" ")[0])
            b.set_fill_color(c)
            b.setToolTip(label)
            b.clicked.connect(lambda _=False, c=c: self._set_color(c.name()))
            crow.addWidget(b)
        self.hex = CustomLineEdit(d.color)
        self.hex.setMaxLength(9)
        self.hex.setFixedWidth(100)
        self.hex.setToolTip("Any color, as #RRGGBB")
        self.hex.textChanged.connect(lambda _t: self._preview.update())
        crow.addWidget(self.hex)
        pick = CustomButton("Pick…")
        pick.clicked.connect(self._pick_color)
        crow.addWidget(pick)
        self._preview = _Swatch(self)
        crow.addWidget(self._preview)
        crow.addStretch(1)
        lay.addLayout(crow)

        lay.addWidget(QLabel("Arguments"))
        head = QHBoxLayout()
        for text, stretch in (("name", 2), ("default", 2), ("choices from", 2), ("custom list", 3)):
            lbl = QLabel(text)
            lbl.setStyleSheet(label_style(self._theme.text().darker(140)))
            head.addWidget(lbl, stretch)
        head.addSpacing(40)
        lay.addLayout(head)
        self.args_box = QVBoxLayout()
        lay.addLayout(self.args_box)
        self.arg_rows: list[_ArgRow] = []
        for a in d.args:
            self.add_arg(a)
        add = CustomButton("+ Add argument")
        add.clicked.connect(lambda: self.add_arg(CustomArg(self._fresh_arg_name(), "0")))
        r = QHBoxLayout()
        r.addWidget(add)
        r.addStretch(1)
        lay.addLayout(r)

        lay.addWidget(QLabel("Code  ({name} is replaced by that argument)"))
        self.template = QPlainTextEdit(d.template)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.template.setFont(f)
        self.template.setMinimumHeight(140)
        lay.addWidget(self.template, 1)
        self.python = CustomCheckBox("Run as embedded Python")
        self.python.setChecked(d.python_on)
        lay.addWidget(self.python)
        self.description = CustomLineEdit(d.description)
        self.description.setPlaceholderText("Tooltip (optional)")
        lay.addWidget(self.description)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(label_style(self._theme.false_color()))
        lay.addWidget(self.error)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel = CustomButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = CustomButton("Save block")
        save.clicked.connect(self._save)
        brow.addWidget(cancel)
        brow.addWidget(save)
        lay.addLayout(brow)
        self.setMinimumWidth(720)
        # plain labels + the code box follow the theme (a frameless dialog
        # doesn't inherit the main window's palette)
        for lbl in self.findChildren(QLabel):
            if not lbl.styleSheet():
                lbl.setStyleSheet(label_style(self._theme.text()))
        pal = self.template.palette()
        pal.setColor(QPalette.Base, self._theme.surface())
        pal.setColor(QPalette.Text, self._theme.text())
        self.template.setPalette(pal)

    def _fresh_arg_name(self) -> str:
        used = {r.name.text().strip() for r in self.arg_rows}
        for n in ("x", "y", "key", "amount", "a", "b", "c"):
            if n not in used:
                return n
        i = 1
        while f"arg{i}" in used:
            i += 1
        return f"arg{i}"

    def add_arg(self, a: CustomArg) -> None:
        row = _ArgRow(self, a)
        self.arg_rows.append(row)
        self.args_box.addWidget(row)

    def remove_arg(self, row: _ArgRow) -> None:
        if row in self.arg_rows:
            self.arg_rows.remove(row)
            row.setParent(None)
            row.deleteLater()

    def _set_color(self, hexv: str) -> None:
        self.hex.setText(hexv)

    def _pick_color(self) -> None:
        start = QColor(self.hex.text()) if QColor.isValidColorName(self.hex.text()) else QColor("#9b6ad6")
        c = QColorDialog.getColor(start, self)
        if c.isValid():
            self._set_color(c.name())

    def color(self) -> QColor:
        t = self.hex.text().strip()
        return QColor(t) if QColor.isValidColorName(t) else self._theme.custom_color()

    def build(self) -> "CustomDef | None":
        name = self.name.text().strip()
        if not name:
            self.error.setText("Give the block a name.")
            return None
        func = custom_blocks.func_name(name)
        args = [r.value() for r in self.arg_rows]
        names = [a.name for a in args]
        for n in names:
            if not n.isidentifier():
                self.error.setText(f"\"{n}\" can't be an argument name (letters, digits and _ only).")
                return None
        if len(set(names)) != len(names):
            self.error.setText("Two arguments have the same name.")
            return None
        clash = self._existing.get(func)
        if clash is not None and clash is not self._original:
            self.error.setText(f"There's already a custom block called \"{clash.name}\".")
            return None
        return CustomDef(name=name, func=func, category=self.category.currentText(), color=self.color().name(),
                         args=args, template=self.template.toPlainText(), python_on=self.python.isChecked(),
                         description=self.description.text().strip())

    def _save(self) -> None:
        d = self.build()
        if d is None:
            return
        ok, msg = custom_blocks.check(d)
        if not ok:
            self.error.setText(f"The code doesn't compile yet: {msg}\n\nWhat the daemon sees:\n{custom_body_code(d)}")
            return
        self.result_def = d
        self.accept()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillPath(rounded_rect_path(QRectF(self.rect()), self._theme.corner_radius(16)), self._theme.page_background())


class _Swatch(QWidget):
    def __init__(self, dialog: CustomBlockDialog):
        super().__init__()
        self.dialog = dialog
        self.setFixedSize(60, 26)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.dialog.color()
        p.setBrush(c)
        p.setPen(c.darker(140))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        p.setPen(contrast_text(c))
        p.drawText(self.rect(), Qt.AlignCenter, "Aa")
