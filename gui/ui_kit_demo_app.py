"""
Reference integration of ui_kit: the same window structure afterglow
uses (sidebar of SegmentButtons -> QStackedWidget of pages, each page
scrollable and outlined, a themed Settings page with ThemeEditorGroup).

Run:  python3 demo_app.py
This file is an example to copy patterns from. It is not part of the kit.
"""
from __future__ import annotations

import sys
from dataclasses import asdict, replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget, QFrame,
)

from ui_kit import (
    CustomButton, CustomCheckBox, CustomGroupBox, SegmentButton, SmoothScrollArea, Theme,
    ThemeEditorGroup, ThemeSettings, compute_scale, crossfade_to_index, paint_page_outline,
    set_settings_provider, show_message,
)

# ---- 1. The app's OWN defaults. Each app has different colors. -----------
# These are the kit's monochrome placeholders. Replace them with the
# colors the user provides for THIS app.
APP_DEFAULTS = ThemeSettings()

# ---- 2. The app's config. A real app loads/saves this from disk and
# caches it (see the guide's performance rules). Here it's in memory.
_current = replace(APP_DEFAULTS)
set_settings_provider(lambda: _current)   # BEFORE any widget is built


class Page(QWidget):
    """A top-level page: its own background + a 3px outline 15% darker
    than that background (afterglow's page_outline rule)."""

    def __init__(self, title: str):
        super().__init__()
        self._theme = Theme()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 3)  # room for the 3px outline
        # EVERY page scrolls, so no page's minimum height can force the
        # whole window taller than the screen (a real afterglow bug).
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        pad = self._theme.padding
        self.body_layout.setContentsMargins(pad, pad, pad, pad)
        self.body_layout.setSpacing(pad)
        heading = QLabel(title)
        heading.setStyleSheet(f"QLabel {{ color: {self._theme.text().name()}; font-size: 18px; font-weight: bold; }}")
        self.body_layout.addWidget(heading)
        scroll.setWidget(self.body)
        # QScrollArea.setWidget() forces autoFillBackground on; turn it
        # back off so the page's own paintEvent background shows through.
        self.body.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        p.fillRect(self.rect(), self._theme.page_background())
        p.end()
        paint_page_outline(self, self._theme.page_background())


class SettingsPage(Page):
    def __init__(self):
        super().__init__("Settings")
        self.editor = ThemeEditorGroup(current=_current, defaults=APP_DEFAULTS)
        self.body_layout.addWidget(self.editor)
        save = CustomButton("Save Settings")
        save.clicked.connect(self._save)
        self.body_layout.addWidget(save, alignment=Qt.AlignRight)
        self.body_layout.addStretch(1)

    def _save(self) -> None:
        bad = self.editor.apply_to(_current)
        # real app: my_config.save() here
        msg = "Saved. Reopen the app to apply everywhere."
        if bad:
            msg += "\n\nIgnored invalid colors: " + ", ".join(bad)
        show_message(self, "Settings", msg)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ui_kit demo")
        self.resize(1280, 800)
        theme = Theme()

        central = QWidget()
        self.setCentralWidget(central)
        # App background via QPalette -- NEVER an unscoped
        # setStyleSheet("background: ...") here: that cascades into every
        # descendant widget and breaks their custom painting.
        central.setAutoFillBackground(True)
        pal = central.palette()
        pal.setColor(central.backgroundRole(), theme.app_background())
        # Plain QLabels (form-row labels etc.) draw with the palette's text
        # roles, NOT the theme. Set them here; palettes inherit down to
        # children safely (unlike stylesheets). Without this, labels use the
        # system palette's text color, which can be dark-on-dark.
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            pal.setColor(role, theme.text())
        central.setPalette(pal)

        row = QHBoxLayout(central)
        pad = theme.padding
        row.setContentsMargins(pad, pad, pad, pad)
        row.setSpacing(pad)

        # Sidebar: page buttons stretch to fill, settings pinned at the bottom.
        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(pad)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.stack = QStackedWidget()

        home = Page("Home")
        box = CustomGroupBox("Example Section")
        lay = box.make_layout(QVBoxLayout)
        lay.addWidget(CustomCheckBox("A custom checkbox"))
        lay.addWidget(CustomButton("A custom button"))
        home.body_layout.addWidget(box)
        home.body_layout.addStretch(1)

        self.nav_buttons = []
        for i, (label, page, stretch) in enumerate(
            [("Home", home, 1), ("Other", Page("Other"), 1), ("Settings", SettingsPage(), 0)]
        ):
            btn = SegmentButton(text=label, position="full")
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if stretch else QSizePolicy.Fixed)
            side.addWidget(btn, stretch=stretch)
            self.nav.addButton(btn, i)
            self.stack.addWidget(page)
            self.nav_buttons.append(btn)
        self.nav_buttons[0].setChecked(True)
        self.nav.idClicked.connect(lambda i: crossfade_to_index(self.stack, i))

        row.addWidget(self.sidebar)
        row.addWidget(self.stack, stretch=1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Size things from the window, not fixed px (afterglow: sidebar
        # is 7% of window width, clamped 64..140; 1920x1080 = scale 1.0).
        self.sidebar.setFixedWidth(max(64, min(140, round(self.width() * 0.07))))
        _ = compute_scale(self.width(), self.height())  # pass to pages that scale fonts/sizes


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
