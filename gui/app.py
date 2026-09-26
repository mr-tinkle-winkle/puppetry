"""
Puppetry's Qt (PySide6) editor. Talks to the C++ daemon only through
shared on-disk config + `systemctl --user restart` and the control
socket, exactly like the old GTK app did with the Python daemon.

Sidebar: Macros / Input Visualizer / Settings. The macro editor opens
over the Macros page (Close returns to it).

CLI (no GUI): `puppetry --name="Macro Name" [args...]`, `puppetry --abort`.
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget,
)

import puppetry_config as cfg
from editor_page import MacroEditorPage
from macro_list_page import MacroListPage
from model import AppModel
from settings_page import SettingsPage
from ui_kit import theme_config
from ui_kit.scale_reveal import crossfade_to_index
from ui_kit.segment_button import SegmentButton
from ui_kit.theme import Theme
from visualizer_page import VisualizerPage
from widgets import ask

PAGE_MACROS, PAGE_VISUALIZER, PAGE_SETTINGS, PAGE_EDITOR = range(4)


def install_theme_provider() -> None:
    theme_config.set_settings_provider(cfg.load_theme_settings)


def apply_window_palette(widget: QWidget, theme: Theme) -> None:
    """Checklist step 4: background + text roles via QPalette, never an
    unscoped stylesheet (pitfall #2); text roles set so plain QLabels are
    readable on any system palette (pitfall #3)."""
    pal = widget.palette()
    pal.setColor(QPalette.Window, theme.app_background())
    pal.setColor(QPalette.Base, theme.page_background())
    pal.setColor(QPalette.AlternateBase, theme.surface())
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(role, theme.text())
    widget.setPalette(pal)
    widget.setAutoFillBackground(True)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Puppetry")
        self.setWindowIcon(QIcon.fromTheme("puppetry"))
        theme = Theme()
        apply_window_palette(self, theme)
        self.model = AppModel()

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        pad = theme.padding
        root.setContentsMargins(pad, pad, pad, pad)
        root.setSpacing(pad)

        sidebar = QWidget()
        sidebar.setFixedWidth(170)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(pad)

        self.stack = QStackedWidget()
        self.macro_page = MacroListPage(self.model, self.open_editor)
        self.visualizer_page = VisualizerPage()
        self.settings_page = SettingsPage(self.model)
        self.editor_page = MacroEditorPage(self.model, self.close_editor)
        for page in (self.macro_page, self.visualizer_page, self.settings_page, self.editor_page):
            self.stack.addWidget(page)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for idx, label in ((PAGE_MACROS, "Macros"), (PAGE_VISUALIZER, "Input Visualizer"), (PAGE_SETTINGS, "Settings")):
            b = SegmentButton(text=label, position="full")
            b.setMinimumHeight(40)
            self.nav.addButton(b, idx)
            side.addWidget(b)
        side.addStretch(1)
        self.nav.button(PAGE_MACROS).setChecked(True)
        self.nav.idClicked.connect(self._nav_clicked)

        root.addWidget(sidebar)
        root.addWidget(self.stack, stretch=1)
        self.resize(1400, 850)

    def _nav_clicked(self, idx: int) -> None:
        if self.stack.currentIndex() == PAGE_EDITOR:
            # Leaving the editor via the sidebar goes through its own
            # unsaved-changes check first.
            if self.editor_page.has_unsaved_changes():
                choice = ask(self, "Unsaved changes", "Save the macro before leaving the editor?",
                             ["Cancel", "Discard", "Save"], default=2)
                if choice == 0 or choice == -1 or (choice == 2 and not self.editor_page.save()):
                    self.nav.button(PAGE_MACROS).setChecked(True)
                    return
            self.editor_page.stop_threads()
        if self.stack.currentIndex() != idx:
            crossfade_to_index(self.stack, idx)

    def open_editor(self, macro_id: str | None) -> None:
        macro = self.model.find(macro_id)
        if macro is not None and macro.get("locked"):
            return  # belt-and-braces; the row's Edit is already disabled
        self.editor_page.load_macro(macro_id)
        crossfade_to_index(self.stack, PAGE_EDITOR)

    def close_editor(self) -> None:
        self.nav.button(PAGE_MACROS).setChecked(True)
        crossfade_to_index(self.stack, PAGE_MACROS)

    def closeEvent(self, event) -> None:
        if self.stack.currentIndex() == PAGE_EDITOR and self.editor_page.has_unsaved_changes():
            if ask(self, "Unsaved changes", "Quit without saving this macro?", ["Cancel", "Quit"]) != 1:
                event.ignore()
                return
        if self.model.dirty and not self.model.state.get("autosave"):
            choice = ask(self, "Unsaved changes", "Save before quitting?", ["Cancel", "Discard", "Save"], default=2)
            if choice == 2:
                self.model.save()
            elif choice != 1:
                event.ignore()
                return
        self.editor_page.stop_threads()
        for row in self.macro_page.rows:
            row.stop_threads()
        super().closeEvent(event)


def run_gui() -> int:
    cfg.ensure_config_exists()
    install_theme_provider()
    app = QApplication(sys.argv)
    app.setApplicationName("puppetry")
    app.setDesktopFileName("puppetry")
    window = MainWindow()
    window.show()
    return app.exec()


def run_cli(argv: list[str]) -> int | None:
    """`--abort`, or `--name="Macro Name" [args...]` (other `--flags` are
    ignored, anything else becomes a macro argument). None = not a CLI call."""
    if "--abort" in argv:
        ok, msg = cfg.send_control_command({"cmd": "ABORT"})
        print("Aborted." if ok else f"puppetry --abort failed: {msg}")
        return 0 if ok else 1
    name, extra = None, []
    for arg in argv:
        if arg.startswith("--name="):
            name = arg[len("--name="):]
        elif not arg.startswith("--"):
            extra.append(arg)
    if name is None:
        return None
    ok, msg = cfg.send_control_command({"cmd": "FIRE", "name": name, "args": extra})
    print(f"Triggered {name!r}." if ok else f"puppetry --name failed: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    result = run_cli(sys.argv[1:])
    if result is not None:
        sys.exit(result)
    sys.exit(run_gui())
