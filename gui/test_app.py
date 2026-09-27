"""
Offscreen tests for the Qt GUI (same QT_QPA_PLATFORM=offscreen approach
as ui_kit_test_kit.py, extended for Puppetry's own screens per the
theming guide). Proves construction, wiring and config round-trips --
NOT how it looks on a real display (guide pitfall #12).

Uses a throwaway HOME so it never touches real config. Uses the real
native binaries from ../native/build when present (macro validation,
transcriber process); those checks are skipped if they aren't built.

Run: QT_QPA_PLATFORM=offscreen python3 test_app.py
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="puppetry_gui_test_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import puppetry_config as cfg

qapp = QApplication.instance() or QApplication([])
QCursor.setPos(4000, 4000)  # offscreen windows under the cursor render hovered (pitfall #12)

_checks, _fail = 0, []


def check(name, cond):
    global _checks
    _checks += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail.append(name)


def pump(ms=0):
    QTest.qWait(ms) if ms else qapp.processEvents()


def main() -> int:
    cfg.ensure_config_exists()
    cfg.save_macros({"macros": [
        {"id": "m1", "name": "fast autoclicker", "code": "speed(0.0000001)\ntap(KEY_SPACE)\nwait(1)\n",
         "combo": ["KEY_HOME"], "repeat_mode": "toggle", "trigger_edge": "down", "python_on": True},
        {"id": "m2", "name": "second", "code": "tap(KEY_A)\n", "combo": [], "repeat_mode": "none",
         "trigger_edge": "down", "python_on": False},
    ]})
    prof = cfg.load_profile("profile_1")
    prof["enabled"] = {"m1": True}
    cfg.save_profile("profile_1", prof)
    st = cfg.load_state()
    st["keyboard_path"], st["mouse_path"] = "/dev/input/event-does-not-exist", "/dev/input/event-nope"
    cfg.save_state(st)

    import app
    import editor_page
    import macro_list_page
    import settings_page
    app.install_theme_provider()
    w = app.MainWindow()
    w.show()
    pump()
    model = w.model

    # ------------------------------------------------------------ layout sanity
    check("window can shrink vertically (pitfall #4: min height < 400)", w.minimumSizeHint().height() < 400)
    rows = w.macro_page.rows
    check("one row per macro", len(rows) == 2)
    r = rows[0]
    lay = r.layout()
    order = [lay.itemAt(i).widget() for i in range(lay.count())]
    expected = [r.name_box, None, r.combo_btn, r.clear_btn, r.edge, r.repeat, r.enabled, r.edit_btn, r.delete_btn, r.lock]
    check("row order: name ... combo x press/release repeat enabled edit delete (lock)", order == expected)
    check("row shows combo", r.combo_btn.text() == "KEY_HOME")
    check("row shows repeat mode", r.repeat.currentText() == "Toggle")
    check("enabled switch reflects the profile", r.enabled.isChecked() and not rows[1].enabled.isChecked())

    # Visual regressions found by looking at real renders:
    from PySide6.QtGui import QFontMetrics
    nb = rows[0].name_box._label
    check("name box is wide enough for its whole name",
          nb.width() - 30 >= QFontMetrics(nb.font()).horizontalAdvance(nb.text))
    rows[1].lock.setChecked(True)
    pump()
    img_on, img_off = rows[0].edit_btn.grab().toImage(), rows[1].edit_btn.grab().toImage()
    y = img_on.height() // 2
    check("a disabled (locked) button looks different from an enabled one",
          img_on.pixelColor(4, y) != img_off.pixelColor(4, y))
    rows[1].lock.setChecked(False)

    # ------------------------------------------------------------ row edits
    r.name_box._begin()
    r.name_box._edit.setText("renamed clicker")
    r.name_box._commit()
    check("click-to-rename updates the macro", model.find("m1")["name"] == "renamed clicker")
    check("rename marks unsaved + shows Save", model.dirty and w.macro_page.save_btn.isVisible())
    rows[1].enabled.setChecked(True)
    check("enabled switch writes the profile", model.is_enabled("m2"))
    r.clear_btn.click()
    check("x clears the combo", model.find("m1")["combo"] == [] and r.combo_btn.text() == "(no combo)")
    r.edge.setCurrentIndex(1)
    r.repeat.setCurrentIndex(1)
    check("row dropdowns write trigger_edge/repeat_mode",
          model.find("m1")["trigger_edge"] == "up" and model.find("m1")["repeat_mode"] == "hold")
    r.lock.setChecked(True)
    check("lock disables rename/combo/edit/delete",
          not any(x.isEnabled() for x in (r.name_box, r.combo_btn, r.edit_btn, r.delete_btn)))
    r.lock.setChecked(False)
    model.save()
    check("Save writes everything to disk",
          cfg.load_macros()["macros"][0]["name"] == "renamed clicker" and cfg.load_profile("profile_1")["enabled"].get("m2"))
    check("Save clears dirty", not model.dirty and not w.macro_page.save_btn.isVisible())

    # ------------------------------------------------------------ editor
    w.open_editor("m1")
    pump(300)
    ed = w.editor_page
    check("editor opens over the macro page", w.stack.currentIndex() == app.PAGE_EDITOR)
    check("editor loads the macro", ed.name_edit.text() == "renamed clicker" and "tap(KEY_SPACE)" in ed.code.toPlainText())
    left_scroll = ed.findChildren(__import__("ui_kit.smooth_scroll_area", fromlist=["x"]).SmoothScrollArea)[0]
    check("editor settings column fits its pane (no horizontal overflow)",
          left_scroll.widget().width() <= left_scroll.viewport().width())
    buttons = [ed.save_btn, ed.save_close_btn, ed.close_btn]
    check("Save / Save and Close / Close in the bottom-right, in that order",
          all(b.isVisible() for b in buttons) and ed.save_btn.x() < ed.save_close_btn.x() < ed.close_btn.x()
          and ed.close_btn.geometry().right() > ed.width() * 0.8 and ed.close_btn.y() > ed.height() * 0.8)

    ed.code.setPlainText("speed(0.0000001)\ntap(KEY_SPACE)\nwait(0.5)\n")
    ed.desc_edit.setText("described")
    have_daemon = cfg.find_binary("puppetry-daemon") is not None
    ok = ed.save()
    check("Save validates + saves", ok and cfg.load_macros()["macros"][0]["code"].endswith("wait(0.5)\n"))
    check("Save does NOT close the editor", w.stack.currentIndex() == app.PAGE_EDITOR)
    check("description round-trips", cfg.load_macros()["macros"][0]["description"] == "described")

    if have_daemon:
        ed.python_cb.setChecked(False)
        ed.code.setPlainText("if True:\n    tap(KEY_A)\n")
        check("python_off macro with control flow is rejected by the daemon's own compiler",
              not ed.save() and "primitives-only" in ed.error.text())
        ed.code.setPlainText("tap(KEY_A, tme_=0.1)\n")
        check("typo'd keyword argument is caught at save time", not ed.save() and "tme_" in ed.error.text())
        ed.python_cb.setChecked(True)
        ed.code.setPlainText("tap(KEY_A\n")
        check("Python syntax errors are caught at save time", not ed.save() and "SyntaxError" in ed.error.text())
    else:
        print("SKIP daemon-backed validation checks (native/build not built)")

    # Close with unsaved edits: Discard
    editor_page.ask = lambda *a, **k: 1
    ed.request_close()
    pump(300)
    check("Close with unsaved changes -> Discard returns to Macros", w.stack.currentIndex() == app.PAGE_MACROS)
    check("discarded edit was NOT saved", "tap(KEY_A" not in cfg.load_macros()["macros"][0]["code"])

    # Save and Close
    w.open_editor("m1")
    pump(300)
    ed.code.setPlainText("tap(KEY_B)\n")
    ed.save_and_close()
    pump(300)
    check("Save and Close saves then closes",
          w.stack.currentIndex() == app.PAGE_MACROS and cfg.load_macros()["macros"][0]["code"] == "tap(KEY_B)\n")

    # New macro
    w.open_editor(None)
    pump(300)
    ed.name_edit.setText("brand new")
    ed.code.setPlainText("tap(KEY_C)\n")
    ed.save_and_close()
    pump(300)
    new = [m for m in cfg.load_macros()["macros"] if m["name"] == "brand new"]
    check("new macro saved with an id, enabled on the current profile",
          len(new) == 1 and new[0]["id"] and cfg.load_profile("profile_1")["enabled"].get(new[0]["id"]))
    check("macro list rebuilt with the new row", len(w.macro_page.rows) == 3)

    # Aliases
    w.open_editor("m1")
    pump(300)
    ed._alias_rows[0][0].setText("jump")
    if ed._alias_rows[0][1].count():
        ed._alias_rows[0][1].setCurrentText("SPACE")
    ed.save()
    check("custom button names saved app-wide",
          ed._alias_rows[0][1].count() == 0 or cfg.load_aliases()["aliases"].get("jump") == "SPACE")

    # Transcription runs the real C++ helper; our fake device paths make it exit "no devices".
    if cfg.find_binary("puppetry-transcribe"):
        ed.tr_kb.setChecked(True)
        ed._transcribe_clicked()
        pump(1500)
        check("transcriber process runs and reports unopenable devices",
              "Couldn't open keyboard/mouse device" in ed.tr_status.text() and ed.tr_btn.text() == "Start Transcribing")
        check("transcription options persist", cfg.load_state().get("transcribe_keyboard") is True)
        ed._insert_transcribed("wait(0.000234)\nkd(KEY_A)\n")
        check("transcribed text is inserted at the cursor", "wait(0.000234)\nkd(KEY_A)" in ed.code.toPlainText())
    else:
        print("SKIP transcriber process check (native/build not built)")

    # Transcribe hotkey: UI round-trips, and the listener starts (and
    # quietly does nothing) even against our fake device paths.
    check("transcribe hotkey starts unset", ed.hotkey_label.text() == "(not set)")
    ed._hotkey_found(ecodes_KEY_F9 := 33, "KEY_F9")  # avoid importing evdev here; any int code will do
    check("transcribe hotkey label updates", ed.hotkey_label.text() == "KEY_F9")
    check("transcribe hotkey persists", cfg.load_state().get("transcribe_hotkey") == "KEY_F9")
    ed._restart_hotkey_listener()
    check("hotkey listener object created once a hotkey + device paths exist", ed._hotkey_listener is not None)
    ed.stop_threads()
    pump(300)
    check("hotkey listener stopped with the rest of the page's threads", ed._hotkey_listener is None)
    editor_page.ask = lambda *a, **k: 1
    ed.request_close()
    pump(300)

    # ------------------------------------------------------------ settings
    w.nav.button(app.PAGE_SETTINGS).click()
    pump(300)
    sp = w.settings_page
    check("settings page shown", w.stack.currentIndex() == app.PAGE_SETTINGS)
    check("profiles listed in settings", sp.profile_rows.count() == 3)
    pid = model.add_profile("Gaming")
    check("new profile appears", sp.profile_rows.count() == 4 and any(n == "Gaming" for _, n in model.ordered_profiles()))
    sp._select_profile(pid)
    check("switching profile changes which macros are enabled",
          model.profile_id == pid and not any(r.enabled.isChecked() for r in w.macro_page.rows))
    check("active profile persisted", cfg.load_state()["active_profile"] == pid)
    model.rename_profile(pid, "Gaming 2")
    check("rename profile", cfg.load_profile(pid)["name"] == "Gaming 2")
    before = [p for p, _ in model.ordered_profiles()]
    model.move_profile(before[1], -1)
    after = [p for p, _ in model.ordered_profiles()]
    check("reorder profiles", after[0] == before[1] and after[1] == before[0])
    settings_page.ask = lambda *a, **k: 1
    sp._delete_profile(pid, "Gaming 2")
    check("delete active profile falls back to another", model.profile_id != pid and not (cfg.PROFILES_DIR / f"{pid}.json").exists())
    sp.show_paths["keyboard"].setChecked(True)
    check("device eye toggle shows the raw path", sp.dev_labels["keyboard"].text() == "/dev/input/event-does-not-exist")
    sp.record_time.setValue(1.5)
    check("record time persists immediately", cfg.load_state()["record_time_seconds"] == 1.5)
    sp.autosave.setChecked(True)
    w.macro_page.rows[0].repeat.setCurrentIndex(2)
    check("autosave: row edits hit disk immediately", cfg.load_macros()["macros"][0]["repeat_mode"] == "toggle" and not model.dirty)
    sp.autosave.setChecked(False)

    # ------------------------------------------------------------ visualizer
    w.nav.button(app.PAGE_VISUALIZER).click()
    pump(300)
    vp = w.visualizer_page
    for _ in range(25):
        QTest.mouseClick(vp.area, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
    QTest.mouseDClick(vp.area, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
    for _ in range(10):
        QTest.keyClick(vp.area, Qt.Key_Space)
    vp._tick()
    check("CPS tester counts clicks (incl. Qt double-click events)", vp.clicks.count >= 26)
    check("CPS tester counts key presses", vp.keys.count == 10)
    check("CPS tester computes a current rate", vp.clicks.current > 0)
    vp.reset()
    check("reset clears totals", vp.clicks.count == 0 and vp.keys.count == 0)

    # ------------------------------------------------------------ styling guardrail
    unscoped = []
    for page in (w.macro_page, w.visualizer_page, w.settings_page, w.editor_page):
        for obj in [page] + page.findChildren(object):
            try:
                sheet = obj.styleSheet()
            except AttributeError:
                continue
            if sheet and not sheet.lstrip().startswith(("Q", "*", "#", ".")):
                unscoped.append(type(obj).__name__)
    check("no unscoped stylesheets (pitfall #2)", unscoped == [])

    w.close()
    print(f"\n{_checks - len(_fail)}/{_checks} checks passed.")
    if _fail:
        print("FAILED:", _fail)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
