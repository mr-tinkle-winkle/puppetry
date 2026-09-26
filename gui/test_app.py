"""
Offscreen structural tests for the Qt port -- same spirit and QT_QPA_PLATFORM=offscreen
approach as ui_kit_test_kit.py, extended for Puppetry's own screens per
UI_THEMING_GUIDE.md section 9 ("extend it for app-specific widgets").
Proves the app constructs, wires signals correctly, and round-trips
config -- NOT that it looks right on a real display (guide pitfall #12:
"Offscreen tests aren't proof it looks right"). A real-desktop visual
check is still owed before calling this screen done.

Run: QT_QPA_PLATFORM=offscreen python3 test_app.py
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Redirect config to a throwaway dir BEFORE importing puppetry_config,
# so this test never touches the real user's ~/.config/macro-daemon.
_tmp_config = tempfile.mkdtemp(prefix="puppetry_test_config_")
os.environ["HOME"] = _tmp_config  # cfg uses Path.home()

sys.path.insert(0, os.path.dirname(__file__))

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

import puppetry_config as cfg

_app = QApplication.instance() or QApplication([])
QCursor.setPos(4000, 4000)  # windows under the offscreen cursor render hovered -- guide pitfall #12

_checks = 0
_failures = []


def check(name: str, cond: bool) -> None:
    global _checks
    _checks += 1
    status = "PASS" if cond else "FAIL"
    print(f"{status} {name}")
    if not cond:
        _failures.append(name)


def main() -> int:
    cfg.ensure_config_exists()

    # Seed one macro so the list/editor have something real to load.
    macros_doc = {"macros": [
        {"id": "abc123", "name": "Test Macro", "code": "kd(KEY_A)\nku(KEY_A)\n",
         "combo": ["KEY_LEFTCTRL", "KEY_F13"], "repeat_mode": "none", "trigger_edge": "down",
         "python_on": True},
    ]}
    cfg.save_macros(macros_doc)
    profile = cfg.load_profile("profile_1")
    profile["enabled"] = {"abc123": True}
    cfg.save_profile("profile_1", profile)

    import app as puppetry_app
    puppetry_app.install_theme_provider()

    window = puppetry_app.MainWindow()
    check("MainWindow constructs without error", window is not None)
    check("Macro list page shows the seeded macro", window.macro_list_page.rows_container.count() == 1)

    # Open the editor for that macro and check fields loaded correctly.
    window._open_editor("abc123")
    editor = window.macro_editor_page
    check("Editor loaded macro name", editor.name_edit.text() == "Test Macro")
    check("Editor loaded macro code", editor.code_edit.toPlainText() == "kd(KEY_A)\nku(KEY_A)\n")
    check("Editor loaded combo", editor._recorded_combo == ["KEY_LEFTCTRL", "KEY_F13"])
    check("Editor loaded repeat_mode", editor.repeat_mode_combo.currentText() == "none")

    # Lock toggle disables the right controls (per the settled actAs-adjacent
    # spec discussion: "Locking disables Edit, Delete, combo recording,
    # and the trigger-edge dropdown until unlocked").
    editor.lock_toggle.setChecked(True)
    check("Lock disables Record Combo", not editor.record_btn.isEnabled())
    check("Lock disables trigger-edge dropdown", not editor.trigger_edge_combo.isEnabled())
    editor.lock_toggle.setChecked(False)
    check("Unlock re-enables Record Combo", editor.record_btn.isEnabled())

    # Editing + Save round-trips to disk.
    editor.name_edit.setText("Renamed Macro")
    editor.code_edit.setPlainText("tap(KEY_B)\n")
    editor._save()
    reloaded = cfg.load_macros()
    saved = next(m for m in reloaded["macros"] if m["id"] == "abc123")
    check("Save persists the new name", saved["name"] == "Renamed Macro")
    check("Save persists the new code", saved["code"] == "tap(KEY_B)\n")

    # New macro flow: no id yet, Save assigns one.
    window._new_macro()
    check("New macro editor starts with default name", editor.name_edit.text() == "New Macro")
    editor.name_edit.setText("Brand New")
    editor.code_edit.setPlainText("pass\n")
    editor._save()
    reloaded2 = cfg.load_macros()
    check("New macro got saved with a real id", any(m["name"] == "Brand New" and m["id"] for m in reloaded2["macros"]))

    # Editor zoom (Ctrl+Scroll) actually changes the font, and Ctrl+0 resets.
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent, Qt as QtNS
    default_size = editor.code_edit.font().pointSize()
    ev = QWheelEvent(QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, 120),
                      QtNS.NoButton, QtNS.ControlModifier, QtNS.NoScrollPhase, False)
    editor.code_edit.wheelEvent(ev)
    check("Ctrl+Scroll changes editor font size", editor.code_edit.font().pointSize() != default_size)

    # Theme.text() readability + no unscoped stylesheets, mirroring the
    # kit's own pitfall-#2 check (an unscoped stylesheet cascades to
    # every descendant and breaks custom painting -- see the guide).
    unscoped = []
    for w in window.findChildren(object):
        try:
            sheet = w.styleSheet()
        except AttributeError:
            continue
        if sheet and not sheet.lstrip().startswith(("Q", "*", "#", ".")):
            unscoped.append((w, sheet))
    check("No unscoped stylesheets anywhere in the window", unscoped == [])

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed.")
    if _failures:
        print("FAILED:", _failures)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
