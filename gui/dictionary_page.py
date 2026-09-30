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
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

import custom_blocks
import reference
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.theme import Theme
from widgets import PageBase, dim_label, label_style, section_title

CATEGORY_NAMES = {"input": "input", "output": "output", "neutral": "neither", "custom": "custom", "function": "function"}


def _mono() -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    return f


class _Entry(QWidget):
    """One dictionary entry: colored signature + description. `haystack`
    is the lower-cased text the search box matches against."""

    def __init__(self, signature: str, description: str, category: str, extra: str = ""):
        super().__init__()
        theme = Theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 6)
        lay.setSpacing(2)
        sig = QLabel(signature)
        sig.setFont(_mono())
        sig.setWordWrap(True)
        sig.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sig.setStyleSheet(label_style(theme.category_color(category), "font-weight: bold;"))
        lay.addWidget(sig)
        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setFont(_mono())
            desc.setStyleSheet(label_style(theme.text().darker(115)))
            lay.addWidget(desc)
        self.category = category
        self.signature = signature
        self.haystack = f"{signature}\n{description}\n{extra}".lower()


class _Section(QWidget):
    def __init__(self, title: str, hint: str = ""):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 8, 0, 0)
        self.lay.setSpacing(2)
        self.lay.addWidget(section_title(title))
        if hint:
            self.lay.addWidget(dim_label(hint))
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


class DictionaryPage(PageBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.content_layout.addWidget(section_title("Dictionary"))
        self.content_layout.addWidget(dim_label(
            "Everything a macro can say. Signatures are colored orange for input, blue for output and gray "
            "for neither. Search matches names and descriptions."))
        self.search = CustomLineEdit("")
        self.search.setPlaceholderText("Search commands, blocks and key names...")
        self.search.textChanged.connect(self.apply_filter)
        self.content_layout.addWidget(self.search)

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
        self.keys = _Section("Key and button names",
                             "Every KEY_* and BTN_* name works as-is; these shorter names are accepted too "
                             "(wherever a key is asked for, in blocks or code).")
        self.notes = _Section("Notes")
        for n in notes:
            lbl = _Entry("", n, "neutral")
            self.notes.add(lbl)
        self.sections = [self.commands, self.language, self.customs, self.keys, self.notes]
        for s in self.sections:
            self.content_layout.addWidget(s)
        self.empty = dim_label("Nothing matches.")
        self.empty.setVisible(False)
        self.content_layout.addWidget(self.empty)
        self.content_layout.addStretch(1)
        self._loaded_extras = False
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
        if not self.keys.entries:
            rows = reference.key_alias_rows()
            for shown, real in rows:
                self.keys.add(_Entry(f"{shown}  ->  {real}", "", "neutral", extra=real))
            if not rows:
                self.keys.add(_Entry("(unavailable)", "Couldn't run puppetry-daemon --dump-names.", "neutral"))
        self.apply_filter()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.refresh_dynamic()

    def apply_filter(self, *_):
        needle = self.search.text().strip().lower()
        total = sum(s.apply(needle) for s in self.sections)
        self.empty.setVisible(bool(needle) and total == 0)
