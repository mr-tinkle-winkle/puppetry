"""
Dictionary: every command, block and key name in one searchable page.

Built from the structured data in reference.py (the same table the block
palette reads), so it can't drift from what the editor and daemon accept.
Each command's signature is colored by category (orange = input, blue =
output, gray = neither); custom blocks (shared by every macro) are listed
too, and the key/button name tables come from the daemon's own
`--dump-names`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QToolTip, QVBoxLayout, QWidget

import custom_blocks
import kbm_layout as kl
import reference
from kbm_paint import KbmView, theme_style
from ui_kit.collapse_toggle_button import CollapseToggleButton
from ui_kit.custom_button import CustomButton
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.theme import Theme
from widgets import PageBase, label_style

CATEGORY_NAMES = {"input": "input", "output": "output", "neutral": "neither", "custom": "custom", "function": "function"}
TEXT_SCALE = 3          # the Dictionary's text is 3x the app's normal size


def base_px() -> float:
    f = QApplication.font()
    return f.pixelSize() if f.pixelSize() > 0 else f.pointSizeF() * 96 / 72


def px(mult: float = 1.0) -> int:
    return max(8, round(base_px() * TEXT_SCALE * mult))


def _mono(size: int) -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setPixelSize(size)
    return f


def _title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(label_style(Theme().text(), f"font-size: {px(1.15)}px; font-weight: bold;"))
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(label_style(Theme().text().darker(140), f"font-size: {px(0.8)}px;"))
    return lbl


class _Header(QWidget):
    """Clickable row: chevron + colored signature."""

    def __init__(self, entry: "_Entry", signature: str, color):
        super().__init__()
        self.entry = entry
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.toggle = CollapseToggleButton(False)
        side = max(24, px(0.8))
        self.toggle.setFixedSize(side, side)
        lay.addWidget(self.toggle, 0, Qt.AlignTop)
        sig = QLabel(signature)
        sig.setFont(_mono(px()))
        sig.setWordWrap(True)
        sig.setStyleSheet(label_style(color, "font-weight: bold;"))
        lay.addWidget(sig, 1)
        self.sig = sig
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.toggle.toggle()


class _Entry(QWidget):
    """One dictionary entry: a colored signature that expands to show its
    description (collapsed by default). `haystack` is what search matches."""

    def __init__(self, signature: str, description: str, category: str, extra: str = ""):
        super().__init__()
        theme = Theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 6)
        lay.setSpacing(2)
        self.desc = None
        if signature:
            self.header = _Header(self, signature, theme.category_color(category))
            lay.addWidget(self.header)
        else:
            self.header = None
        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            df = QFont()
            df.setPixelSize(px(0.8 if signature else 0.75))
            desc.setFont(df)
            desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
            desc.setStyleSheet(label_style(theme.text().darker(115)))
            desc.setContentsMargins(max(24, px(0.8)) + 8 if signature else 0, 0, 0, 0)
            lay.addWidget(desc)
            self.desc = desc
            if self.header:
                desc.setVisible(False)
                self.header.toggle.toggled.connect(self._toggled)
        self.user_expanded = False
        self.category = category
        self.signature = signature
        self.haystack = f"{signature}\n{description}\n{extra}".lower()

    def _toggled(self, on: bool) -> None:
        self.desc.setVisible(on)

    def expanded(self) -> bool:
        return bool(self.header and self.header.toggle.isChecked())

    def set_expanded(self, on: bool) -> None:
        if self.header and self.desc and self.header.toggle.isChecked() != on:
            self.header.toggle.setChecked(on)


class _Section(QWidget):
    def __init__(self, title: str, hint: str = ""):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 8, 0, 0)
        self.lay.setSpacing(2)
        self.lay.addWidget(_title(title))
        if hint:
            self.lay.addWidget(_hint(hint))
        self.entries: list = []

    def add(self, entry: QWidget) -> None:
        self.entries.append(entry)
        self.lay.addWidget(entry)

    def clear(self) -> None:
        for e in self.entries:
            self.lay.removeWidget(e)
            e.setParent(None)
            e.deleteLater()
        self.entries = []

    def apply(self, needle: str) -> int:
        shown = 0
        for e in self.entries:
            ok = (not needle) or all(w in e.haystack for w in needle.split())
            e.setVisible(ok)
            shown += ok
        self.setVisible(shown > 0 or not self.entries and not needle)
        return shown


class KeyNameMap(KbmView):
    """The Input Visualizer's keyboard + mouse with every key's names written
    on it: the short name(s) you can type big, the KEY_/BTN_ name small.
    Hover a key for all of its names; search lights matching keys up."""

    def __init__(self, parent=None):
        style = theme_style(Theme())
        style.update(show_arrow=False, show_timers=False, font_scale=26, padding=4)
        super().__init__(style, parent=parent)
        self.setMouseTracking(True)
        self.aliases: dict = {}
        self.names = {}

    def set_aliases(self, rows: list) -> None:
        """rows: [(shown names "A / a", real KEY_ name)]"""
        self.aliases = {real: [n.strip() for n in shown.split("/")] for shown, real in rows}
        names = {}
        for it in self.layout_["items"]:
            n = it["name"]
            if not n:
                continue
            al = self.aliases.get(n) or []
            big = al[0] if al else (it["label"] or n.replace("KEY_", "").replace("BTN_", ""))
            names[n] = (big, n)
        self.names = names
        self.update()

    def haystack(self, name: str) -> str:
        return " ".join([name] + self.aliases.get(name, [])).lower()

    def search(self, needle: str) -> int:
        if not needle:
            self.highlight = set()
        else:
            self.highlight = {n for n in self.names if all(w in self.haystack(n) for w in needle.split())}
        self.update()
        return len(self.highlight)

    def item_at(self, pos):
        u = self.unit()
        from kbm_paint import picture_size
        w, h = picture_size(self.layout_, self.style_, u)
        ox = (self.width() - w) / 2 + float(self.style_.get("padding", 0))
        oy = (self.height() - h) / 2 + float(self.style_.get("padding", 0))
        x, y = (pos.x() - ox) / u, (pos.y() - oy) / u
        for it in reversed(self.layout_["items"]):
            if it["name"] and it["x"] <= x <= it["x"] + it["w"] and it["y"] <= y <= it["y"] + it["h"]:
                return it
        return None

    def tooltip_for(self, it) -> str:
        n = it["name"]
        al = self.aliases.get(n)
        return f"{n}" + (f"\nalso: {', '.join(al)}" if al else "")

    def mouseMoveEvent(self, e) -> None:
        it = self.item_at(e.position())
        if it:
            QToolTip.showText(e.globalPosition().toPoint(), self.tooltip_for(it), self)
        else:
            QToolTip.hideText()


class DictionaryPage(PageBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.content_layout.addWidget(_title("Dictionary"))
        self.content_layout.addWidget(_hint(
            "Everything a macro can say. Signatures are colored orange for input, blue for output and gray "
            "for neither -- click one to see what it does. Search matches names and descriptions."))
        row = QHBoxLayout()
        self.search = CustomLineEdit("")
        self.search.setPlaceholderText("Search commands, blocks and key names...")
        f = self.search.font()
        f.setPixelSize(px(0.8))
        self.search.setFont(f)
        self.search.setMinimumHeight(px(1.3))
        self.search.textChanged.connect(self.apply_filter)
        row.addWidget(self.search, 1)
        for text, on in (("Expand all", True), ("Collapse all", False)):
            b = CustomButton(text)
            b.clicked.connect(lambda _=False, on=on: self.expand_all(on))
            row.addWidget(b)
        self.content_layout.addLayout(row)

        self.commands = _Section("Commands")
        entries, notes = reference.parse_dictionary()
        for e in entries:
            self.commands.add(_Entry(e["signature"], e["description"], e["category"],
                                     extra=" ".join(e["names"]) + " " + reference.PRIMITIVES_BY_NAME.get(
                                         e["name"], reference.Primitive(e["name"], (), "neutral")).label))
        self.language = _Section("Blocks and code", "Control flow, variables and conditions.")
        for title, code, desc, cat in reference.LANGUAGE_ENTRIES:
            self.language.add(_Entry(title, f"{code}\n{desc}", cat))
        self.customs = _Section("Custom blocks", "Made with \"Create a custom block\"; shared by every macro.")
        self.keys_title = _Section("Key and button names",
                                   "Big: the short name you can type (wherever a key is asked for). Small: its "
                                   "KEY_/BTN_ name, which always works too. Hover a key for every name it has.")
        self.keymap = KeyNameMap()
        self.keymap.setMinimumHeight(300)
        self.keys_title.lay.addWidget(self.keymap)
        self.other_keys = _Section("Other key names", "Keys that aren't on the picture above.")
        self.notes = _Section("Notes")
        for n in notes:
            self.notes.add(_Entry("", n, "neutral"))
        self.sections = [self.commands, self.language, self.customs, self.other_keys, self.notes]
        for s in (self.commands, self.language, self.customs, self.keys_title, self.other_keys, self.notes):
            self.content_layout.addWidget(s)
        self.empty = _hint("Nothing matches.")
        self.empty.setVisible(False)
        self.content_layout.addWidget(self.empty)
        self.content_layout.addStretch(1)
        self._keys_loaded = False
        self._auto_expanded: set = set()
        self.refresh_dynamic()

    # custom blocks and key tables can change while the app runs
    def refresh_dynamic(self) -> None:
        self.customs.clear()
        defs = custom_blocks.load()
        for d in defs.values():
            args = ", ".join(a.name for a in d.args)
            self.customs.add(_Entry(f"{d.func}({args})", f"{d.name}  [{d.category}]  {d.description}".strip(), "custom"))
        if not defs:
            self.customs.add(_Entry("(none yet)", "Use the button under \"My blocks\" in the block editor.", "custom"))
        if not self._keys_loaded:
            self._keys_loaded = True
            rows = reference.key_alias_rows()
            self.keymap.set_aliases(rows)
            on_map = set(self.keymap.names)
            for shown, real in rows:
                if real not in on_map:
                    self.other_keys.add(_Entry(f"{shown}  ->  {real}", "", "neutral", extra=real))
            if not rows:
                self.other_keys.add(_Entry("(unavailable)", "Couldn't run puppetry-daemon --dump-names.", "neutral"))
        self.apply_filter()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.refresh_dynamic()

    def all_entries(self):
        for s in self.sections:
            yield from s.entries

    def expand_all(self, on: bool) -> None:
        self._auto_expanded.clear()
        for e in self.all_entries():
            e.set_expanded(on)

    def apply_filter(self, *_):
        needle = self.search.text().strip().lower()
        total = sum(s.apply(needle) for s in self.sections)
        keys = self.keymap.search(needle)
        self.keys_title.setVisible(not needle or keys > 0)
        total += keys
        self.empty.setVisible(bool(needle) and total == 0)
        # while searching, open entries whose match is in the (hidden) description
        for e in list(self._auto_expanded):
            if not needle or e.isHidden():
                e.set_expanded(False)
                self._auto_expanded.discard(e)
        if needle:
            for e in self.all_entries():
                if not e.isHidden() and not e.expanded() and e.header and e.desc:
                    if not all(w in e.signature.lower() for w in needle.split()):
                        e.set_expanded(True)
                        self._auto_expanded.add(e)
