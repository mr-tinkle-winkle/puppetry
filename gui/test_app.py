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
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="puppetry_gui_test_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from ui_kit.custom_button import CustomButton

import puppetry_config as cfg
from ui_kit.theme import Theme

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


BLOCK_ROUND_TRIP_SAMPLES = [
    "", "tap(KEY_A)\n", "tap(KEY_A)", "tap( KEY_A ,0.2)\n",
    "arguments(hits=3, key=KEY_A)\nfor _ in range(int(hits)):\n    tap(key)\n    wait(0.05)\n",
    "# header\n\ntap(KEY_A)  # trailing\n\n\nwait(1)\n# end\n\n",
    "x = 5\nx += 1\nwhile x > 0:\n    x -= 1\n    if x == 2:\n        tap(KEY_B)\n    elif x == 3:  # three\n"
    "        pass\n    else:\n        # nothing\n        wait(0.1)\n",
    "for i in range(3):\n    move_mouse(10*i, 0, time_=0.1, easing=\"linear\")\n    # inner\n# outer\ntap(KEY_C)\n",
    "combo(KEY_LEFTCTRL, KEY_S, time_=0.2)\nactAs(BTN_LEFT, False, KEY_Q, KEY_E)\ncommand(\"notify-send {0}\", \"hi\")\n",
    "def f():\n    pass\nimport os\nprint('x'); tap(KEY_A)\nif True: tap(KEY_B)\n",
    "type(\"\"\"multi\nline\"\"\")\nfor _ in range(2):\n    type(\"\"\"a\nb\"\"\")\n",
    "if True:\n\ttap(KEY_A)\n\twhile False:\n\t\tpass\n",
    "checkpoint()\nspeed(2)\nignore(\"keyboard\")\nignore_keys(KEY_W, BTN_LEFT)\nkd(KEY_A)\nku(KEY_A)\nwheel(-3)\n",
    "second(1, 2)\nUnknown_Thing()\n",
    "x = [1,\n  2]\ntap(KEY_A,\n    time_=0.3)\n",
    "try:\n    tap(KEY_A)\nexcept Exception:\n    pass\n",
    "tap(key=KEY_A, time_=0.1)\n", "# only comments\n# here\n", "type(\"héllo ✓\")  # ünïcode\n",
]


def block_tests(w, ed, model) -> None:
    import block_model as bm
    import block_render as br
    import custom_blocks
    import editor_page
    from block_model import Rep
    from reference import PRIMITIVES, PRIMITIVES_BY_NAME

    # ------------------------------------------------------------ model: round-trip
    bad = [s for s in BLOCK_ROUND_TRIP_SAMPLES if not bm.round_trips(s, ["second"])]
    check("blocks: code -> blocks -> code is byte-for-byte for every sample", bad == [])
    if bad:
        print("   round-trip failures:", bad)
    d = bm.code_to_blocks("import os\ntry:\n    tap(KEY_A)\nexcept Exception:\n    pass\nwith open('x') as f: pass\n")
    check("blocks: unrecognized statements become custom-code blocks (nothing dropped)",
          [b.kind for b in d.main] == ["raw", "raw", "raw"] and "except Exception:" in d.main[1].fields["code"])
    d = bm.code_to_blocks("a; b\nif x: tap(KEY_A)\n")
    check("blocks: one-line compound/semicolon statements stay custom code", [b.kind for b in d.main] == ["raw", "raw"])
    try:
        bm.code_to_blocks("for _ in range(3:\n")
        check("blocks: a syntax error raises BlockParseError", False)
    except bm.BlockParseError as exc:
        check("blocks: a syntax error raises BlockParseError", exc.line == 1)

    # ------------------------------------------------------------ model: reporters
    d = bm.code_to_blocks("x = 0\nif x >= 5:\n    pass\nwhile KEY_A in getButtonsHeld():\n    pass\n"
                          "p = getMousePosition()\ny = getMousePosition.y\nb = waitForPress(repress=True)\n"
                          "tap(b, time_=0.1)\ntype('a', async_=True)\n")
    cond = d.main[1].fields["cond0"]
    check("reporters: a comparison becomes a compare reporter with a variable inside",
          isinstance(cond, Rep) and cond.kind == "compare" and cond.name == ">=" and cond.fields["left"] == Rep("var", "x")
          and cond.fields["right"] == "5")
    check("reporters: `key in getButtonsHeld()` becomes \"key is held\"",
          d.main[2].fields["cond"] == Rep("held", "", {"key": "KEY_A"}))
    check("reporters: mouse position (and .y)", d.main[3].fields["value"] == Rep("mouse", "")
          and d.main[4].fields["value"] == Rep("mouse", "y"))
    press = d.main[5].fields["value"]
    check("reporters: b = waitForPress(...) holds a \"key pressed\" reporter",
          isinstance(press, Rep) and press.kind == "press" and press.fields["repress"] == Rep("bool", "True"))
    check("reporters: True/False are true/false reporters", d.main[7].fields["async_"] == Rep("bool", "True"))
    for b in bm.walk(d.main):
        b.touch()
    regen = bm.blocks_to_code(d)
    check("reporters regenerate valid, equivalent code",
          "if x >= 5:" in regen and "while KEY_A in getButtonsHeld():" in regen and "y = getMousePosition.y" in regen
          and "b = waitForPress(repress=True)" in regen and "type('a', async_=True)" in regen)
    t = Theme()
    check("true is green, false is red", br.rep_color(Rep("bool", "True"), t) == t.true_color()
          and br.rep_color(Rep("bool", "False"), t) == t.false_color() and t.true_color() != t.false_color())

    # ------------------------------------------------------------ model: list sockets
    cb = bm.new_call("combo")
    check("list sockets start with the starter values", cb.fields["keys"] == ["KEY_LEFTCTRL", "KEY_C"])
    bm.set_socket(cb, ("keys", 2, ()), "KEY_V")
    check("filling the + slot appends", cb.fields["keys"] == ["KEY_LEFTCTRL", "KEY_C", "KEY_V"])
    bm.set_socket(cb, ("keys", 0, ()), "")
    check("clearing a list element removes it", cb.fields["keys"] == ["KEY_C", "KEY_V"])
    ar = bm.new_block("arguments")
    bm.set_socket(ar, ("params", 1, ()), "key=KEY_A")
    check("arguments grows the same way", bm.blocks_to_code([ar]) == "arguments(count=3, key=KEY_A)\n")
    cmp_ = bm.new_rep("compare")
    ib = bm.new_block("if")
    ib.fields["cond0"] = cmp_
    bm.set_socket(ib, ("cond0", None, ("left",)), Rep("var", "n"))
    bm.set_socket(ib, ("cond0", None, ("right",)), "3")
    check("nested sockets inside a comparison", bm.blocks_to_code([ib]) == "if n == 3:\n    pass\n")

    # ------------------------------------------------------------ model: codegen per block type
    def gen(*blocks):
        return bm.blocks_to_code(list(blocks))

    tap = bm.new_call("tap")
    check("codegen: tap", gen(tap) == "tap(KEY_A)\n")
    tap.fields["time_"] = "0.2"
    check("codegen: optional param positional", gen(tap) == "tap(KEY_A, 0.2)\n")
    tap.kw.add("time_")
    check("codegen: param that was a keyword stays a keyword", gen(tap) == "tap(KEY_A, time_=0.2)\n")
    cb = bm.new_call("combo")
    cb.fields["time_"] = "0.3"
    check("codegen: combo's time_ is always a keyword", gen(cb) == "combo(KEY_LEFTCTRL, KEY_C, time_=0.3)\n")
    check("codegen: actAs positional + varargs", gen(bm.new_call("actAs")) == "actAs(KEY_A, False, KEY_B)\n")
    mm = bm.new_call("move_mouse")
    mm.fields["easing"] = '"linear"'
    check("codegen: skipped optional -> later ones become keywords", gen(mm) == 'move_mouse(100, 0, easing="linear")\n')
    check("codegen: checkpoint/wait", gen(bm.new_call("checkpoint"), bm.new_call("wait")) == "checkpoint()\nwait(0.1)\n")
    check("codegen: wait for press / reactivation",
          gen(bm.new_call("waitForPress"), bm.new_call("waitForReactivation")) == "waitForPress()\nwaitForReactivation()\n")
    rep = bm.new_block("repeat")
    rep.bodies[0].append(bm.new_call("kd"))
    check("codegen: repeat nests its children", gen(rep) == "for _ in range(10):\n    kd(KEY_A)\n")
    check("codegen: empty mouth emits pass", gen(bm.new_block("while")) == "while True:\n    pass\n")
    ie = bm.new_block("if", has_else=True)
    ie.fields["cond0"] = Rep("bool", "True")
    ie.bodies[1].append(bm.new_call("ku"))
    check("codegen: if/else", gen(ie) == "if True:\n    pass\nelse:\n    ku(KEY_A)\n")
    fo = bm.new_block("for")
    check("codegen: for each", gen(fo) == "for i in range(10):\n    pass\n")
    ch = bm.new_block("change")
    ch.fields["op"] = "*"
    check("codegen: set / change / note / arguments / run macro",
          gen(bm.new_block("arguments"), bm.new_block("assign"), ch, bm.new_block("comment"),
              bm.new_block("macro_call", name="second"))
          == "arguments(count=3)\nx = 0\nx *= 1\n# note\nsecond()\n")
    mc = bm.new_block("macro_call", name="second")
    mc.fields["args"] = ["5", Rep("var", "n")]
    check("codegen: run macro with arguments", gen(mc) == "second(5, n)\n")
    d = bm.code_to_blocks("tap( KEY_A ,0.2)\n")
    d.main[0].fields["time_"] = "0.5"
    d.main[0].touch()
    check("codegen: an edited block is regenerated from its sockets", bm.blocks_to_code(d) == "tap(KEY_A, 0.5)\n")

    # ------------------------------------------------------------ model: functions + notes
    code = ("arguments(n=2)\n\ndef burst(k, times=3):\n    for _ in range(times):\n        tap(k)\n    return times\n\n"
            "burst(KEY_A)\n#@note 300,40: remember to test this\n")
    d = bm.code_to_blocks(code)
    check("functions: a top-level def becomes its own function stack",
          [f.name for f in d.functions] == ["burst"] and d.functions[0].fields["params"] == ["k", "times=3"]
          and [b.kind for b in d.main] == ["arguments", "func_call"])
    check("functions: return is a block", d.functions[0].bodies[0][-1].kind == "return")
    check("notes: #@note lines become free-floating notes", len(d.notes) == 1 and d.notes[0].text == "remember to test this"
          and (d.notes[0].x, d.notes[0].y) == (300, 40))
    check("functions + notes round-trip byte-for-byte", bm.blocks_to_code(d) == code)
    d.notes[0].x = 123
    check("a moved note saves its new position", "#@note 123,40: remember to test this" in bm.blocks_to_code(d))

    # ------------------------------------------------------------ custom blocks
    cdef = bm.CustomDef(name="click at", func="click_at", category="Output", color="#123456",
                        args=[bm.CustomArg("x", "0"), bm.CustomArg("y", "0"), bm.CustomArg("btn", "BTN_LEFT", "mouse")],
                        template="move_mouse({x}, {y}, time_=0, move_to=True)\ntap({btn}, time_=0)\n")
    check("custom block body: arguments() + template with {arg} -> arg",
          bm.custom_body_code(cdef) == "arguments(x=0, y=0, btn=BTN_LEFT)\nmove_mouse(x, y, time_=0, move_to=True)\n"
                                       "tap(btn, time_=0)\n")
    customs = {"click_at": cdef}
    d = bm.code_to_blocks("click_at(10, 20)\n", customs=customs)
    check("custom block calls parse into custom blocks", d.main[0].kind == "custom" and d.main[0].fields["x"] == "10"
          and d.main[0].fields["btn"] == "BTN_LEFT")
    cb2 = bm.new_block("custom", defn=cdef)
    check("custom block codegen passes every argument", bm.blocks_to_code([cb2], customs=customs) == "click_at(0, 0, BTN_LEFT)\n")
    custom_blocks.save(customs)
    check("custom blocks save/load", custom_blocks.load()["click_at"].template == cdef.template
          and custom_blocks.load()["click_at"].args[2].source == "mouse")
    if cfg.find_binary("puppetry-daemon"):
        ok, msg = custom_blocks.check(cdef)
        check("a custom block's code compiles with the daemon (native path)", ok)
        bad_def = bm.CustomDef(name="bad", func="bad", template="tap({nope})\n")
        check("a broken custom block is rejected", not custom_blocks.check(bad_def)[0])

    # ------------------------------------------------------------ primitive table vs daemon
    src = (Path(__file__).resolve().parent.parent / "native" / "src" / "python_embed.cpp").read_text()
    import re as _re
    native = set(_re.findall(r'FC\("([A-Za-z_]+)"', src))
    check("primitive table matches the daemon's registered primitives exactly",
          native == {p.name for p in PRIMITIVES})
    check("every primitive has an input/output/neutral category",
          all(p.category in ("input", "output", "neutral") for p in PRIMITIVES))
    check("block labels are plain English (no camelCase / snake_case)",
          all(p.title == p.title.lower() and "_" not in p.title for p in PRIMITIVES)
          and PRIMITIVES_BY_NAME["actAs"].title == "act as" and PRIMITIVES_BY_NAME["move_mouse"].title == "move mouse")

    # everything the palette makes compiles on the NATIVE path, control flow included
    if cfg.find_binary("puppetry-daemon"):
        failures = []
        for p in PRIMITIVES:
            if p.reporter:
                continue
            code = bm.blocks_to_code([bm.new_call(p.name)])
            ok, msg = cfg.check_macro({"name": "t", "code": code, "python_on": False})
            if not ok:
                failures.append((p.name, msg))
        check("every palette primitive block compiles on the native path", failures == [])
        cmpif = bm.new_block("if", has_else=True)
        cmpif.fields["cond0"] = Rep("compare", ">", {"left": Rep("var", "x"), "right": "1"})
        fdef = bm.new_block("def", name="helper")
        fdef.fields["params"] = ["a"]
        fdef.bodies[0] = [bm.new_block("return")]
        fdef.bodies[0][0].fields["value"] = Rep("var", "a")
        doc = bm.Doc(main=[bm.new_block("arguments"), bm.new_block("assign"), bm.new_block("change"), rep, cmpif, fo,
                           bm.new_block("func_call", name="helper")], functions=[fdef],
                     notes=[bm.Note("a note", 5, 5)])
        doc.main[-1].fields["args"] = ["1"]
        full = bm.blocks_to_code(doc)
        for py in (False, True):
            ok, msg = cfg.check_macro({"name": "t", "code": full, "python_on": py})
            check(f"control flow / variables / functions / notes compile ({'python' if py else 'native'})", ok)

    # ------------------------------------------------------------ editor: views
    odd = "tap( KEY_A ,0.2)\n\n# note\nfor _ in range(2):\n  wait(0.1)  # two-space indent\n"
    cfg.save_macros({"macros": model.macros_data["macros"]})
    m = model.find("m1")
    m["code"], m["python_on"] = odd, True
    ed.load_macro("m1")
    be = ed.blocks
    w.resize(2000, 1100)
    from PySide6.QtWidgets import QSplitter
    ed.findChild(QSplitter).setSizes([300, 1700])
    pump(50)
    check("editor opens macros in Block view by default", ed.view_mode() == "blocks"
          and ed.code_stack.currentWidget() is be and ed.blocks_view_btn.isChecked())
    check("the hat shows the macro's trigger combo", be.hat_text.startswith("when "))
    check("no horizontal scroll bar when the blocks fit", be.view.horizontalScrollBar().maximum() == 0)
    ed.set_view_mode("text")
    ed.set_view_mode("blocks")
    ed.set_view_mode("text")
    check("just looking at blocks never rewrites the code", ed.code.toPlainText() == odd and not ed.has_unsaved_changes())
    ed.set_view_mode("blocks")

    titles = [t for t, _c, _e in __import__("block_editor").palette_sections(be)]
    check("palette has a Conditions section", any(t.startswith("Conditions") for t in titles))
    tc = next(e for t, _c, e in __import__("block_editor").palette_sections(be) if t.startswith("Timing"))
    check("true and false live under Timing & control",
          any(x[0] == "rep" and x[1] == Rep("bool", "True") for x in tc) and any(x[0] == "rep" and x[1] == Rep("bool", "False") for x in tc))

    def lb_of(block):
        for it in be._items:
            lay = getattr(it, "layout", None)
            for lb in (lay.blocks if lay else []):
                if lb.block is block:
                    return it, lb
        return None, None

    v, vpt = be.view, be.view.viewport()

    def vpos(it, x, y):
        return v.mapFromScene(it.mapToScene(QPointF(x, y)))

    def drag(p0, p1, steps=10, mods=Qt.NoModifier):
        QTest.mousePress(vpt, Qt.LeftButton, mods, p0)
        for k in range(1, steps + 1):
            QTest.mouseMove(vpt, p0 + (p1 - p0) * k / steps)
        QTest.mouseRelease(vpt, Qt.LeftButton, mods, p1)
        pump()

    # tooltips everywhere
    missing = [lb.block.kind for it in be._items for lb in getattr(it, "layout", br.StackLayout([], [], 0, 0)).blocks
               if not be.tip_for(__import__("block_editor").Hit(it, lb, None, "block"))]
    check("every block on the canvas has a hover tooltip", missing == [])

    # drag the loop out onto empty canvas -> loose, not part of the macro
    it, lb = lb_of(be.doc.main[2])
    p0 = vpos(it, lb.x + 4, lb.y + 10)
    drag(p0, p0 + QPoint(420, 160))
    check("dragging a block away detaches it and everything below", ed.code_text() == "tap( KEY_A ,0.2)\n\n# note\n"
          and be.loose_count() == 2)
    check("loose blocks are called out as not saved", "won't be saved" in be.loose_label.text())
    it, lb = lb_of(be.doc.loose[0].blocks[0])
    cit, clb = lb_of(be.doc.main[1])
    p0 = vpos(it, lb.x + 4, lb.y + 10)
    p1 = vpos(cit, clb.x + 4, clb.y + clb.h + 12)
    QTest.mousePress(vpt, Qt.LeftButton, Qt.NoModifier, p0)
    for k in range(1, 11):
        QTest.mouseMove(vpt, p0 + (p1 - p0) * k / 10)
    check("a snap ghost shows while a block hovers near a notch", be.ghost is not None and be.snap is not None)
    QTest.mouseRelease(vpt, Qt.LeftButton, Qt.NoModifier, p1)
    pump()
    check("dropping near a notch snaps it back in (original text kept)", ed.code_text() == odd and be.loose_count() == 0)

    # Ctrl-drag takes just the one block
    it, lb = lb_of(be.doc.main[0])
    p0 = vpos(it, lb.x + 4, lb.y + 10)
    drag(p0, p0 + QPoint(500, 300), mods=Qt.ControlModifier)
    check("Ctrl-drag moves only that block", ed.code_text().startswith("\n# note\nfor _") and be.loose_count() == 1)
    be.undo()
    check("undo puts it back", ed.code_text() == odd and be.loose_count() == 0)
    be.redo()
    check("redo moves it again", be.loose_count() == 1)
    be.undo()

    # a lone note dragged to empty canvas becomes a free-floating note -- and is saved
    it, lb = lb_of(be.doc.main[1])
    p0 = vpos(it, lb.x + 4, lb.y + 10)
    drag(p0, p0 + QPoint(600, 40), mods=Qt.ControlModifier)
    check("a note dropped on empty canvas floats free (not a loose block) and is saved",
          len(be.doc.notes) == 1 and be.loose_count() == 0 and "#@note " in ed.code_text()
          and ed.code_text().rstrip().endswith(": note"))
    note_item = next(i for i in be._items if isinstance(i, __import__("block_editor").NoteItem))
    p0 = v.mapFromScene(note_item.mapToScene(QPointF(10, 10)))
    it, lb = lb_of(be.doc.main[0])
    p1 = vpos(it, lb.x + 12, lb.y + lb.h + 12)
    drag(p0, p1)
    check("dragging a free note into a stack attaches it again", not be.doc.notes
          and be.doc.main[1].kind == "comment" and "#@note" not in ed.code_text())
    be.undo()
    be.undo()
    check("(undo back to the start)", ed.code_text() == odd)

    # the hat moves the whole script
    x0 = be.doc.main_x
    hat_lb = be.main_item.layout.blocks[0]
    p0 = v.mapFromScene(be.main_item.mapToScene(QPointF(hat_lb.x + 30, hat_lb.y + 10)))
    drag(p0, p0 + QPoint(80, 30))
    check("dragging \"when ... pressed\" moves the script", abs(be.doc.main_x - (x0 + 80)) < 2 and ed.code_text() == odd)

    # click a socket -> inline editor -> Enter commits
    it, lb = lb_of(be.doc.main[0])
    fh = lb.fields[0]
    QTest.mouseClick(vpt, Qt.LeftButton, Qt.NoModifier, vpos(it, fh.rect.center().x(), fh.rect.center().y()))
    pump()
    from PySide6.QtWidgets import QLineEdit
    editors = [c for c in vpt.findChildren(QLineEdit) if c.isVisible()]
    check("clicking a socket opens an inline editor", len(editors) == 1 and editors[0].text() == "KEY_A")
    if editors:
        editors[0].setText("KEY_Z")
        QTest.keyClick(editors[0], Qt.Key_Return)
        pump()
    check("editing a socket rewrites just that block", ed.code_text().startswith("tap(KEY_Z, 0.2)\n\n# note\nfor _ in range(2):\n  wait(0.1)  # two-space indent"))
    check("block edits mark the macro unsaved", ed.has_unsaved_changes())

    # reporters: drop a variable in, then drag it back OUT
    it, lb = lb_of(be.doc.main[0])
    be.rep_drop(Rep("var", "x"), it.mapToScene(lb.fields[0].rect.center()))
    check("dropping a variable onto a socket uses it", ed.code_text().startswith("tap(x, 0.2)"))
    it, lb = lb_of(be.doc.main[0])
    vf = lb.fields[0]
    p0 = vpos(it, vf.rect.center().x(), vf.rect.center().y())
    QTest.mousePress(vpt, Qt.LeftButton, Qt.NoModifier, p0)
    for k in range(1, 9):
        QTest.mouseMove(vpt, p0 + QPoint(40 * k, 30 * k))
    lifted = be.drag is not None and be.drag[0] == "rep"
    QTest.mouseRelease(vpt, Qt.LeftButton, Qt.NoModifier, p0 + QPoint(320, 240))
    pump()
    check("a reporter can be dragged OUT of its socket (dropped on nothing = removed)",
          lifted and ed.code_text().startswith("tap(, 0.2)") is False and be.doc.main[0].fields["key"] == "")
    be.undo()
    be.undo()
    check("(undo restores it)", ed.code_text().startswith("tap(KEY_Z, 0.2)"))

    # list sockets in the UI: the + slot
    be.add_blocks([bm.new_call("combo")])
    it, lb = lb_of(be.doc.main[-1])
    plus = [f for f in lb.fields if f.kind == "append"]
    check("a list socket shows one empty + slot", len(plus) == 1 and plus[0].ref == ("keys", 2, ()))
    be.set_socket(be.doc.main[-1], plus[0].ref, "KEY_V")
    it, lb = lb_of(be.doc.main[-1])
    plus = [f for f in lb.fields if f.kind == "append"]
    check("...filling it adds another", plus[0].ref == ("keys", 3, ()) and ed.code_text().endswith("combo(KEY_LEFTCTRL, KEY_C, KEY_V)\n"))
    be.undo()
    be.undo()

    # python_on is NOT forced any more: the native path runs loops/ifs itself
    ed.python_cb.setChecked(False)
    it, lb = lb_of(be.doc.main[0])
    be.external_drop({"spec": {"kind": "repeat"}, "hx": 10, "hy": 10},
                     it.mapToScene(QPointF(lb.x + 10, lb.y + lb.h + 10)))
    pump()
    check("palette drop snaps a new block in", ed.code_text().startswith("tap(KEY_Z, 0.2)\nfor _ in range(10):\n  pass\n"))
    check("new blocks follow the macro's own indentation (2 spaces here)", "\n  pass\n" in ed.code_text())
    check("a loop leaves \"Run as embedded Python\" alone (native runs it)", not ed.python_cb.isChecked())
    if cfg.find_binary("puppetry-daemon"):
        check("...and the macro saves on the native path", ed.save())
    ed.python_cb.setChecked(True)

    # functions from the palette
    be.external_drop({"spec": {"kind": "def"}, "hx": 10, "hy": 10}, QPointF(900, 80))
    pump()
    check("dropping \"create function\" makes a function stack", [f.name for f in be.doc.functions] == ["my_function"]
          and "def my_function():\n  pass\n" in ed.code_text())
    fit = next(i for i in be._items if getattr(i, "role", "") == "def")
    be.external_drop({"spec": {"kind": "call", "name": "tap"}, "hx": 10, "hy": 10},
                     fit.mapToScene(QPointF(4, fit.layout.blocks[0].h + 4)))
    pump()
    check("blocks snap under a function's hat", "def my_function():\n  tap(KEY_A)\n" in ed.code_text())
    be.add_blocks([bm.new_block("func_call", name="my_function")])
    check("run function calls it", ed.code_text().endswith("my_function()\n"))
    be.undo(); be.undo(); be.undo()

    # arguments() only ever snaps to the very top of the macro
    args = [bm.new_block("arguments")]
    it, lb = lb_of(be.doc.main[1])
    be._find_snap(it.mapToScene(QPointF(lb.x, lb.y + lb.h)), args, 40)
    check("arguments() won't snap mid-macro", be.snap is None)
    be._find_snap(be.main_item.mapToScene(QPointF(0, br.MIN_H)), args, 40)
    check("arguments() snaps right under the hat", be.snap is not None and be.snap.index == 0
          and be.snap.container is be.doc.main)
    be.clear_hover_state()
    be.undo()   # the loop drop

    # delete + context helpers
    n = len(be.doc.main)
    be.duplicate_block(be.doc.main[0])
    check("duplicate inserts a copy right after", len(be.doc.main) == n + 1 and ed.code_text().startswith("tap(KEY_Z, 0.2)\ntap(KEY_Z, 0.2)"))
    be.delete_block(be.doc.main[1])
    check("delete removes just that block", len(be.doc.main) == n)
    loop = next(b for b in be.doc.main if b.kind == "repeat" and b.bodies[0])
    be.to_text_block(loop)
    raw = next(b for b in be.doc.main if b.kind == "raw")
    check("Edit as custom code turns a block into a custom code block", raw.fields["code"].startswith("for _ in range(2):"))
    check("custom code blocks are purple", br.block_color(raw, Theme()) == Theme().custom_color())
    be.raw_to_blocks(raw)
    check("...and Turn into blocks parses it back", any(b.kind == "repeat" for b in be.doc.main))

    # custom blocks through the dialog
    import custom_block_dialog
    dlg = custom_block_dialog.CustomBlockDialog(be, None, be.customs, be.key_names)
    dlg.name.setText("double tap")
    dlg.category.setCurrentText("Output")
    dlg._set_color("#aa3344")
    dlg.arg_rows[0].name.setText("key")
    dlg.arg_rows[0].default.setText("KEY_A")
    dlg.arg_rows[0].source.setCurrentIndex(1)
    dlg.template.setPlainText("tap({key}, time_=0)\ntap({key}, time_=0)\n")
    built = dlg.build()
    check("custom block dialog builds the definition", built is not None and built.func == "double_tap"
          and built.color == "#aa3344" and built.args[0].source == "keys")
    if cfg.find_binary("puppetry-daemon"):
        dlg._save()
        check("custom block dialog validates with the daemon before saving", dlg.result_def is not None)
    be.customs[built.func] = built
    custom_blocks.save(be.customs)
    be.palette_widget.refresh(force=True)
    out_sec = next(e for t, _c, e in __import__("block_editor").palette_sections(be) if t.startswith("Output"))
    check("a custom block shows up in the category it picked",
          any(x[0] == "block" and x[1].get("func") == "double_tap" for x in out_sec))
    from PySide6.QtWidgets import QApplication as _QA
    _QA.processEvents()
    pw = be.palette_widget
    check("palette content never wider than its viewport (no clipped reporters)",
          pw.sizeHint().width() <= be.palette_scroll.viewport().width())
    from ui_kit.custom_scrollbar import CustomScrollBar as _CSB
    check("smooth scroll areas use matching custom bars on both axes",
          isinstance(be.palette_scroll.horizontalScrollBar(), _CSB))
    be.add_blocks([bm.new_block("custom", defn=built)])
    check("custom blocks generate a call", ed.code_text().endswith("double_tap(KEY_A)\n"))
    if cfg.find_binary("puppetry-daemon"):
        check("a macro using a custom block still saves", ed.save())
    be.undo()

    # text <-> blocks after edits
    ed.set_view_mode("text")
    check("switching to Text shows the regenerated code", ed.code_stack.currentWidget() is ed.code
          and "tap(KEY_Z, 0.2)" in ed.code.toPlainText())
    ed.code.setPlainText("for _ in range(3:\n")
    check("unparseable code stays in Text, with the reason", not ed.set_view_mode("blocks")
          and ed.view_mode() == "text" and "syntax error" in ed.error.text())
    ed.code.setPlainText("while True:\n    tap(KEY_Q)\n")
    check("fixed code switches to blocks", ed.set_view_mode("blocks") and [b.kind for b in be.doc.main] == ["while"])

    # transcription in Block view appends, re-reads, and undoes as ONE step
    before = ed.code_text()
    ed._tr_join = False
    be.begin_external_edit()
    ed._insert_transcribed("wait(0.25)\n")
    ed._insert_transcribed("kd(KEY_A)\n")
    ed._reparse_timer.stop()
    ed._reparse_after_transcription()
    check("transcribed lines show up as blocks", [b.kind for b in be.doc.main] == ["while", "call", "call"]
          and ed.code_text().endswith("wait(0.25)\nkd(KEY_A)\n"))
    be.undo()
    check("one undo removes the whole transcription (Block view)", ed.code_text() == before)
    # ...and in Text view: the text box's own undo
    ed.set_view_mode("text")
    ed.code.setPlainText("tap(KEY_A)\ncheckpoint()\nold()\n")
    ed.tr_clear.setChecked(True)
    ed._tr_join = False
    ed._apply_clear_before_transcribing()
    ed._insert_transcribed("new1()\n")
    ed._insert_transcribed("new2()\n")
    after = ed.code.toPlainText()
    ed.code.undo()
    check("one undo removes the whole transcription, clearing included (Text view)",
          after == "tap(KEY_A)\ncheckpoint()\nnew1()\nnew2()\n" and ed.code.toPlainText() == "tap(KEY_A)\ncheckpoint()\nold()\n")
    ed.tr_clear.setChecked(False)
    ed.code.setPlainText("while True:\n    tap(KEY_Q)\n")
    ed.set_view_mode("blocks")

    # loose blocks aren't saved, and Save says so
    be.doc.loose.append(bm.Stack([bm.new_call("tap")], 600, 300))
    be._rebuild()
    ok = ed.save()
    check("save with loose blocks saves only the attached ones and says so",
          ok and "loose block" in ed.error.text() and "tap(KEY_A)" not in cfg.load_macros()["macros"][0]["code"])

    # icons: text until the art exists, then recolored SVG
    from ui_kit import icons
    check("no icon files yet -> None (callers fall back to text)", icons.icon_pixmap("nav_macros") is None
          or icons.icon_path("nav_macros") is not None)
    tmp_icons = Path(tempfile.mkdtemp())
    (tmp_icons / "nav_test.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                                           '<rect width="10" height="10" fill="currentColor"/></svg>')
    old_dir, icons.ICON_DIR = icons.ICON_DIR, tmp_icons
    pm = icons.icon_pixmap("nav_test", 16, QColor("#ff0000"))
    img = pm.toImage() if pm else None
    check("an SVG icon loads and is recolored to the requested color",
          img is not None and img.pixelColor(img.width() // 2, img.height() // 2).name() == "#ff0000")
    icons.ICON_DIR = old_dir
    check("every expected icon name is documented", all(n in (icons.__doc__ or "") for n in icons.EXPECTED))

    # the input/output colors are used outside the block editor too
    t = Theme()
    check("record combo / key pickers / transcribe are input-orange",
          all(b._fill_override == t.input_color() for b in (ed.record_btn, ed.ping_btn, ed.tr_btn)))
    check("sound previews are output-blue", all(b._fill_override == t.output_color() for b in ed.sound_test_btns))
    check("macro rows' combo buttons are input-orange",
          all(r.combo_btn._fill_override == t.input_color() for r in w.macro_page.rows))

    # global default: Text
    model.set_pref("editor_default_mode", "text")
    ed.load_macro("m1")
    check("with the default set to Text, macros open as text", ed.view_mode() == "text"
          and ed.code_stack.currentWidget() is ed.code)
    check("...and can still be switched to Blocks per macro", ed.set_view_mode("blocks") and ed.view_mode() == "blocks")
    model.set_pref("editor_default_mode", "blocks")
    m = model.find("m1")
    m["code"] = "if (\n"
    ed.load_macro("m1")
    check("a macro whose code can't parse opens in Text instead", ed.view_mode() == "text")


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
    check("+ New Macro sits at the bottom-right of the Macros page",
          w.macro_page.new_btn.geometry().right() > w.macro_page.width() * 0.8
          and w.macro_page.new_btn.y() > w.macro_page.height() * 0.7)

    # ------------------------------------------------------------ editor persistence
    # Leaving the editor via the sidebar (not Save/Close/Discard) keeps
    # in-progress edits; re-opening the very same macro resumes them
    # instead of reloading from disk and losing them.
    w.open_editor("m2")
    pump(200)
    ed = w.editor_page
    ed.name_edit.setText("in progress rename")
    app.ask = lambda *a, **k: 1  # "Discard" here only means "don't save before leaving"
    w._nav_clicked(app.PAGE_SETTINGS)
    pump(200)
    w._nav_clicked(app.PAGE_MACROS)
    pump(200)
    w.open_editor("m2")
    pump(200)
    check("re-opening the same macro resumes unsaved edits instead of reloading",
          w.stack.currentIndex() == app.PAGE_EDITOR and ed.name_edit.text() == "in progress rename")

    # But when nothing's actually unsaved, re-opening the same macro still
    # picks up a change made elsewhere (e.g. renaming it from the list)
    # instead of trusting the last-shown state forever.
    ed.load_macro("m2")  # test scaffolding: force a clean reload, bypassing the editor's own Save/Close
    check("editor is clean right after a reload", not ed.has_unsaved_changes())
    model.set_field("m2", "name", "renamed from the list")
    w.open_editor("m2")  # already showing m2, with nothing unsaved
    pump(200)
    check("re-opening the same (clean) macro still picks up an external rename",
          ed.name_edit.text() == "renamed from the list")
    model.set_field("m2", "name", "second")  # restore, so later checks aren't thrown off
    w.close_editor()  # back to the Macros page, so later visibility-based checks see it current
    pump(200)

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
    left_scroll = ed.left_scroll
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
        ed.code.setPlainText("import os\n")
        check("python_off macro with unsupported Python is rejected by the daemon's own compiler",
              not ed.save() and "isn't supported" in ed.error.text())
        ed.code.setPlainText("x = 2\nif x > 1:\n    tap(KEY_B)\n")
        check("python_off macro with control flow now compiles (native interpreter)", ed.save())
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

    # ------------------------------------------------------------ Macro Editor sidebar entry
    check("Macro Editor sits in the sidebar directly under Macros",
          w.nav.button(app.PAGE_MACROS).text() == "Macros"
          and w.nav.button(app.PAGE_EDITOR).text() == "Macro Editor"
          and w.nav.button(app.PAGE_EDITOR).y() > w.nav.button(app.PAGE_MACROS).y()
          and w.nav.button(app.PAGE_EDITOR).y() < w.nav.button(app.PAGE_VISUALIZER).y())

    # With a macro currently loaded in the editor, clicking the sidebar
    # entry from elsewhere just shows it (same persistence as the Edit button).
    w.open_editor("m1")
    pump(200)
    w._nav_clicked(app.PAGE_SETTINGS)
    pump(200)
    w._nav_clicked(app.PAGE_EDITOR)
    pump(200)
    check("Macro Editor nav entry re-shows the macro already loaded",
          w.stack.currentIndex() == app.PAGE_EDITOR and w.nav.button(app.PAGE_EDITOR).isChecked())

    # With nothing selected (a blank editor), it bounces back to Macros
    # instead of opening an empty page, and shows the toast.
    ed.macro = editor_page.blank_macro()
    w._nav_clicked(app.PAGE_EDITOR)
    pump(200)
    check("Macro Editor nav entry with nothing selected redirects to Macros",
          w.stack.currentIndex() == app.PAGE_MACROS and w.nav.button(app.PAGE_MACROS).isChecked())
    check("...and shows the 'Please select a macro' toast",
          w.macro_page.toast.text() == "Please select a macro.")

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

        ed.tr_ignore_puppetry.setChecked(True)
        ed.tr_ignore_alttab.setChecked(True)
        check("ignore-puppetry/ignore-alt-tab toggles persist",
              cfg.load_state().get("transcribe_ignore_puppetry") is True
              and cfg.load_state().get("transcribe_ignore_alttab") is True)

        ed.tr_clear.setChecked(True)
        ed.code.setPlainText("leftover content\n")
        ed._transcribe_clicked()  # start: should clear first, synchronously, before the process even runs
        check("clear-before-transcribing wipes the code box on start", ed.code.toPlainText() == "")
        check("clear-before-transcribing persists", cfg.load_state().get("transcribe_clear_before") is True)
        pump(1500)
        ed.transcriber.stop()
        pump(300)
    else:
        print("SKIP transcriber process check (native/build not built)")

    # Transcription start/finish sounds. Nothing is actually played here
    # (no audio in the sandbox) -- what's checked is the wiring: the paths
    # persist, the labels follow them, and a start/stop routes to the right
    # cue. sound.play is stubbed so the checks don't depend on a player
    # being installed.
    import sound
    real_play = sound.play
    played = []
    sound.play = lambda path: (played.append(path), None)[1]
    check("sounds start out unset",
          ed.sound_labels["start"].text() == "(none)" and ed.sound_labels["finish"].text() == "(none)")
    ed._set_sound("start", "/tmp/ding.wav")
    ed._set_sound("finish", "/tmp/done.mp3")
    check("sound paths persist", cfg.load_state().get("transcribe_start_sound") == "/tmp/ding.wav"
          and cfg.load_state().get("transcribe_finish_sound") == "/tmp/done.mp3")
    check("label shows the file name, tooltip the full path",
          ed.sound_labels["start"].text() == "ding.wav"
          and ed.sound_labels["start"].toolTip() == "/tmp/ding.wav")
    played.clear()
    ed._transcribe_stopped("Stopped.")  # whatever the reason, the finish cue plays
    check("finish sound plays when transcription stops", played == ["/tmp/done.mp3"])
    played.clear()
    ed._test_sound("start")
    check("the test button plays the start sound", played == ["/tmp/ding.wav"])
    # A cue that can't play is reported, never raised -- it must not get in
    # the way of the thing it was announcing.
    sound.play = lambda path: "No audio player found on PATH."
    ed._transcribe_stopped("Stopped.")
    check("an unplayable sound reports instead of raising",
          "No audio player found" in ed.tr_status.text())
    sound.play = real_play
    check("a missing sound file is reported, not played", "not found" in (sound.play("/tmp/nope-does-not-exist.wav") or ""))
    check("an empty path is a silent no-op", sound.play("") is None)
    ed._set_sound("start", "")
    ed._set_sound("finish", "")
    check("clearing a sound empties it", ed.sound_labels["start"].text() == "(none)"
          and cfg.load_state().get("transcribe_start_sound") == "")

    # "Ignore Puppetry" focus reporting: Qt already knows when our own
    # window is focused, so the helper is told over stdin instead of
    # spawning kdotool twice per poll to ask KWin. No process needed to
    # check the wiring -- and reporting with nothing running must be a
    # harmless no-op, since focus changes whenever the user alt-tabs.
    import transcription
    tc = transcription.TranscriptionController()
    tc.report_focus(True)  # no process: must not raise
    sent = []
    tc.report_focus = lambda focused: sent.append(focused)
    tc._watch_focus()
    check("focus watcher connects and reports the state it starts in",
          tc._focus_connected and sent == [qapp.focusWindow() is not None])
    tc._focus_changed(None)
    tc._focus_changed(w.windowHandle())
    check("focus changes map to focused/unfocused reports", sent[-2:] == [False, True])
    tc._unwatch_focus()
    check("focus watcher disconnects on stop", not tc._focus_connected)
    before = len(sent)
    tc._focus_changed(None)  # still routed directly, but the signal is gone
    qapp.focusWindowChanged.emit(None)
    check("no reports once disconnected", len(sent) == before + 1)

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

    # Restart-transcription hotkey: its own picker, own pref key, own
    # listener -- deliberately independent of the toggle hotkey above.
    check("restart key starts unset", ed.restart_label.text() == "(not set)")
    ed._restart_key_found(34, "KEY_F10")
    check("restart key label updates", ed.restart_label.text() == "KEY_F10")
    check("restart key persists under its own pref, not transcribe_hotkey",
          cfg.load_state().get("transcribe_restart_key") == "KEY_F10"
          and cfg.load_state().get("transcribe_hotkey") == "KEY_F9")
    ed._rearm_restart_key_listener()
    check("restart key listener object created once a restart key + device paths exist",
          ed._restart_key_listener is not None)
    check("restart key listener is a separate object from the toggle hotkey's",
          ed._restart_key_listener is not ed._hotkey_listener)
    ed.stop_threads()
    pump(300)
    check("restart key listener also stopped by stop_threads", ed._restart_key_listener is None)
    ed._restart_key_found(None, "")  # simulate "no key detected" (Esc during picking)
    check("declining the restart key picker keeps the previous value", ed.restart_label.text() == "KEY_F10")

    # Checkpoint key: its own picker/pref too, but no listener of its own --
    # the transcriber process itself watches for it while recording.
    check("checkpoint key starts unset", ed.checkpoint_label.text() == "(not set)")
    ed._checkpoint_key_found(35, "KEY_F11")
    check("checkpoint key label updates", ed.checkpoint_label.text() == "KEY_F11")
    check("checkpoint key persists", cfg.load_state().get("transcribe_checkpoint_key") == "KEY_F11")
    ed._checkpoint_key_found(None, "")
    check("declining the checkpoint key picker keeps the previous value", ed.checkpoint_label.text() == "KEY_F11")

    # "Clear macro before transcribing", checkpoint-aware: with no
    # checkpoint() line, wipes everything (old behavior); with one, only
    # what comes after the LAST checkpoint() is cleared, and the cursor
    # lands right after it.
    ed.tr_clear.setChecked(True)
    ed.code.setPlainText("tap(KEY_A)\ntap(KEY_B)\n")
    ed._apply_clear_before_transcribing()
    check("no checkpoint(): clears the whole macro", ed.code.toPlainText() == "")
    ed.code.setPlainText("tap(KEY_A)\ncheckpoint()\ntap(KEY_B)\ncheckpoint()\ntap(KEY_C)\n")
    ed._apply_clear_before_transcribing()
    check("with checkpoint(): keeps up to and including the LAST one",
          ed.code.toPlainText() == "tap(KEY_A)\ncheckpoint()\ntap(KEY_B)\ncheckpoint()\n")
    check("cursor moves to the end of the kept text", ed.code.textCursor().position() == len(ed.code.toPlainText()))
    ed.tr_clear.setChecked(False)
    ed.code.setPlainText("tap(KEY_A)\ncheckpoint()\ntap(KEY_B)\n")
    ed._apply_clear_before_transcribing()
    check("clear-before-transcribing off: code left untouched",
          ed.code.toPlainText() == "tap(KEY_A)\ncheckpoint()\ntap(KEY_B)\n")

    # Restart transcription: only does anything while one's already
    # running -- a no-op from a standing stop, unlike the toggle hotkey
    # which would start one. While running, it stops then starts fresh.
    ed.tr_clear.setChecked(False)
    stopped_calls, started_calls = [], []
    ed.transcriber.stop = lambda: stopped_calls.append(True)
    ed.transcriber.running = lambda: False
    ed._start_transcription = lambda: started_calls.append(True)
    ed._restart_transcription()
    check("restart is a no-op when nothing is transcribing",
          stopped_calls == [] and started_calls == [])
    ed.transcriber.running = lambda: True
    ed._restart_transcription()
    check("restart stops then starts fresh when one is already running",
          stopped_calls == [True] and started_calls == [True])

    block_tests(w, ed, model)

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
    # autosave + throttling: the first save after a quiet second is
    # immediate; rapid follow-ups are batched into ONE save + daemon
    # restart a second after the last one.
    import model as model_mod
    restarts = []
    real_restart = cfg.restart_daemon_service
    cfg.restart_daemon_service = lambda: (restarts.append(1), (True, "Saved. Daemon restarted."))[1]
    sp.autosave.setChecked(True)
    model._last_save = 0.0
    model._save_timer.stop()
    restarts.clear()
    w.macro_page.rows[0].repeat.setCurrentIndex(2)
    check("autosave: the first edit hits disk immediately",
          cfg.load_macros()["macros"][0]["repeat_mode"] == "toggle" and not model.dirty and restarts == [1])
    w.macro_page.rows[0].repeat.setCurrentIndex(1)
    pump(300)
    w.macro_page.rows[0].repeat.setCurrentIndex(0)
    pump(300)
    w.macro_page.rows[0].enabled.setChecked(not w.macro_page.rows[0].enabled.isChecked())
    check("autosave: quick follow-up edits wait (nothing saved or restarted yet)",
          restarts == [1] and cfg.load_macros()["macros"][0]["repeat_mode"] == "toggle" and model.dirty)
    pump(700)
    check("...each new edit restarts the one-second wait", restarts == [1])
    pump(600)
    check("...then ONE save writes them all and restarts the daemon once",
          restarts == [1, 1] and cfg.load_macros()["macros"][0]["repeat_mode"] == "none" and not model.dirty)
    w.macro_page.rows[0].repeat.setCurrentIndex(1)
    model.flush_pending_save()
    check("a pending batched save is flushed (e.g. on quit)", cfg.load_macros()["macros"][0]["repeat_mode"] == "hold"
          and not model.save_pending())
    cfg.restart_daemon_service = real_restart
    sp.autosave.setChecked(False)

    # Pointer acceleration: on by default (the daemon writes kcminputrc for
    # its own virtual mouse), and the opt-out persists.
    check("flat pointer acceleration defaults to on", sp.flat_accel.isChecked())
    sp.flat_accel.setChecked(False)
    check("pointer-acceleration opt-out persists", cfg.load_state().get("disable_pointer_accel") is False)
    sp.flat_accel.setChecked(True)
    check("...and back on again", cfg.load_state().get("disable_pointer_accel") is True)

    # Which view the macro editor opens in (Blocks / Text) is a global pref.
    check("macro editor defaults to opening in Blocks", sp.default_view.currentText() == "Blocks")
    sp.default_view.setCurrentIndex(1)
    check("default editor view persists", cfg.load_state().get("editor_default_mode") == "text")
    sp.default_view.setCurrentIndex(0)
    check("...and back to Blocks", cfg.load_state().get("editor_default_mode") == "blocks")

    # Real-time priority: off by default (it needs a privilege the service
    # unit has to grant), and opting in persists.
    check("real-time priority defaults to off", not sp.realtime.isChecked())
    sp.realtime.setChecked(True)
    check("real-time priority opt-in persists", cfg.load_state().get("realtime_priority") is True)
    sp.realtime.setChecked(False)

    # ------------------------------------------------------------ wheel never changes dropdowns / spin boxes
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtCore import QPointF as _QPF
    w.nav.button(app.PAGE_MACROS).click()
    pump(200)
    combo = w.macro_page.rows[0].repeat
    before_idx = combo.currentIndex()

    def wheel(widget, dy):
        ev = QWheelEvent(_QPF(5, 5), _QPF(widget.mapToGlobal(QPoint(5, 5))), QPoint(0, 0), QPoint(0, dy),
                         Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        QApplication.sendEvent(widget, ev)

    wheel(combo, -120)
    wheel(combo, 120)
    wheel(combo, -240)
    check("scrolling over a dropdown doesn't change it", combo.currentIndex() == before_idx)
    spin = sp.record_time
    v0 = spin.value()
    wheel(spin, 120)
    wheel(spin.lineEdit(), 120)
    check("scrolling over a spin box doesn't change it", spin.value() == v0)

    # ------------------------------------------------------------ categories
    mp = w.macro_page
    n_rows = len(mp.rows)
    check("no categories yet -> no headers (plain list)", not mp.headers)
    cname = model.add_category("Gaming")
    first_id = mp.rows[0].macro_id
    model.set_macro_category(first_id, cname)
    check("a category groups its macros under a header", "Gaming" in mp.headers and "" in mp.headers
          and len(mp.rows) == n_rows)
    order = [mp.rows_box.itemAt(i).widget() for i in range(mp.rows_box.count())]
    gaming_idx = order.index(mp.headers["Gaming"])
    check("...with its macro right under it", any(r.macro_id == first_id for r in order[gaming_idx + 1:]
                                                     if isinstance(r, macro_list_page.MacroRow)))
    model.set_enabled(first_id, True)
    mp.headers["Gaming"].enabled.setChecked(False)
    check("switching a category off turns its macros off (switch remembered)",
          not model.is_effectively_enabled(first_id) and model.is_enabled(first_id)
          and next(r for r in mp.rows if r.macro_id == first_id).graphicsEffect() is not None)
    model.save()
    check("category on/off is saved in macros.json", cfg.load_macros()["categories"] == [{"name": "Gaming", "enabled": False}]
          and next(m for m in cfg.load_macros()["macros"] if m["id"] == first_id)["category"] == "Gaming")
    check("categories are separate from profiles", "categories" not in cfg.load_profile(model.profile_id))
    mp.headers["Gaming"].toggle.setChecked(False)
    check("collapsing a category hides its rows", not next(r for r in mp.rows if r.macro_id == first_id).isVisibleTo(mp))
    model.rename_category("Gaming", "Games")
    check("renaming a category keeps its macros", (model.find(first_id) or {}).get("category") == "Games"
          and model.is_collapsed("Games"))
    model.set_collapsed("Games", False)
    model.delete_category("Games")
    check("deleting a category moves its macros to Uncategorized", "category" not in (model.find(first_id) or {})
          and not mp.headers)
    model.save()

    # ------------------------------------------------------------ profile switch from the Macros page
    import widgets as widgets_mod
    menus = []
    orig_menu = macro_list_page.QMenu

    class _FakeMenu(orig_menu):
        def exec(self, *a):
            menus.append([(act.text(), act.isChecked()) for act in self.actions()])
            target = next((act for act in self.actions() if not act.isChecked()), None)
            if target is not None:
                target.trigger()
    macro_list_page.QMenu = _FakeMenu
    before = model.profile_id
    mp.profile_label.click()
    pump(100)
    macro_list_page.QMenu = orig_menu
    check("clicking \"Profile: ...\" lists the profiles with the current one ticked",
          menus and any(checked for _t, checked in menus[0]) and len(menus[0]) == len(model.ordered_profiles()))
    check("...and picking one switches profile", model.profile_id != before
          and mp.profile_label.text().startswith("Profile: "))
    widgets_mod.switch_profile_interactive(mp, model, before)

    # ------------------------------------------------------------ colors: purple / green / red
    t = Theme()
    row = mp.rows[0]
    check("default buttons are the general purple", t.button_color() == t.general_color() and row.edit_btn._fill_override is None)
    check("delete / clear buttons are red", row.delete_btn._fill_override == t.disabled_color()
          and row.clear_btn._fill_override == t.disabled_color())
    lock = widgets_mod.LockToggle(True)
    check("a locked macro's lock is red", lock._fill_override == t.disabled_color())
    img_on = widgets_mod.ToggleSwitch(True)
    img_on.resize(46, 26)
    img_on._pos = 1.0
    pic = img_on.grab().toImage()
    c = pic.pixelColor(8, 13)
    check("an ON switch is green", c.green() > c.red() + 30)
    img_off = widgets_mod.ToggleSwitch(False)
    img_off.resize(46, 26)
    pic = img_off.grab().toImage()
    c = pic.pixelColor(38, 13)
    check("an OFF switch is red", c.red() > c.green() + 20)
    d = widgets_mod.ThemedDialog("x", "", ["Cancel", "Delete"])
    btns = d.findChildren(CustomButton)
    check("Delete in a confirm dialog is red, Cancel isn't",
          any(b.text() == "Delete" and b._fill_override == t.disabled_color() for b in btns)
          and any(b.text() == "Cancel" and b._fill_override is None for b in btns))

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

    # macro-equivalent readout + drawn keyboard/mouse
    import input_transcript as it
    import visualizer_page as vz
    vp.clear_macro()
    QTest.keyClick(vp.area, Qt.Key_A)
    QTest.mouseClick(vp.area, Qt.RightButton, Qt.NoModifier, QPoint(30, 30))
    vp.area.wheelEvent(__import__("PySide6.QtGui", fromlist=["QWheelEvent"]).QWheelEvent(
        QPointF(30, 30), QPointF(30, 30), QPoint(0, 0), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
        Qt.NoScrollPhase, False))
    vp._tick()
    shown = vp.macro_text.toPlainText()
    check("visualizer: a key press shows up as tap(KEY_A)", "tap(KEY_A" in shown)
    check("visualizer: a mouse click shows up as tap(BTN_RIGHT)", "tap(BTN_RIGHT" in shown)
    check("visualizer: scrolling shows up as wheel(1)", "wheel(1)" in shown)
    vp.combine.setChecked(False)
    vp._tick()
    check("visualizer: combine off gives exact kd/ku lines",
          "kd(KEY_A)" in vp.macro_text.toPlainText() and "ku(KEY_A)" in vp.macro_text.toPlainText())
    vp.combine.setChecked(True)
    QTest.keyPress(vp.area, Qt.Key_W)
    check("visualizer: a held key lights up on the drawn keyboard", "KEY_W" in vp.area.held_names)
    QTest.keyRelease(vp.area, Qt.Key_W)
    check("visualizer: the keyboard layout covers every letter",
          all(f"KEY_{c}" in vz.CODE_TO_NAME.values() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    vp.clear_macro()
    check("visualizer: clear empties the readout", vp.macro_text.toPlainText() == "")

    import kbm_layout as kl
    QTest.keyPress(vp.area, Qt.Key_W)
    pump(120)
    held_for = time.perf_counter() - vp.area.kbm.held.get("KEY_W", time.perf_counter())
    check("visualizer: a held key runs a timer (label replaced by seconds.milliseconds)",
          held_for > 0.05 and vp.area.wants_repaint(time.perf_counter())
          and kl.format_hold(held_for).count(".") == 1)
    QTest.keyRelease(vp.area, Qt.Key_W)
    for i in range(12):
        QTest.mouseMove(vp.area, QPoint(400 - i * 12, 200))
        pump(5)
    for i in range(6):
        QTest.mouseMove(vp.area, QPoint(256, 200 - i * 10))
        pump(5)
    mo = vp.area.kbm.motion
    check("visualizer: mouse movement is recorded for the movement views (left, then up)",
          len(mo) > 4 and mo[-1][1] < mo[0][1] and mo[-1][2] < mo[0][2])
    fr = kl.motion_frame(vp.area.kbm, "comet", 200, {"trail_seconds": 1.0, "trail_width": 4.0}, time.perf_counter(),
                         "t", "tail")
    check("visualizer: the comet has a trail and a dot", len(fr["trail"]) > 2 and fr["dot"] is not None)
    vp.area.repaint()

    # ---- OBS & replay overlay section
    import overlay_config as oc
    ov = vp.overlay
    ov.full_toggle.setChecked(True)
    ov.simple_toggle.setChecked(True)
    ov.mouse_toggle.setChecked(True)
    ov.replay_toggle.setChecked(True)
    ov.flush()
    saved = oc.load()
    check("overlay: toggles are saved to overlay.json", saved["full"]["enabled"] and saved["simple"]["enabled"]
          and saved["simple"]["mouse_movement"] and saved["replay"]["enabled"])
    check("overlay: URLs shown for enabled pages", ov.full_url.text() == "http://127.0.0.1:17380/"
          and ov.mouse_url.isEnabled())
    from overlay_settings import CustomizeDialog
    dlg = CustomizeDialog(ov, "full")
    dlg.form.widgets["unit"].setValue(33)
    dlg.form.widgets["show_timers"].setChecked(False)
    ov.flush()
    saved = oc.load()
    check("overlay: Customize edits the style live (no restart needed)", saved["style"]["unit"] == 33
          and saved["style"]["show_timers"] is False)
    check("overlay: every schema option has a control", set(dlg.form.widgets) == set(oc.DEFAULTS["style"]))
    dlg.preview.repaint()
    dlg._reset()
    ov.flush()
    check("overlay: reset to defaults", oc.load()["style"]["unit"] == oc.DEFAULTS["style"]["unit"])
    from PySide6.QtWidgets import QScrollArea
    for k in ("full", "simple", "movement"):
        d2 = CustomizeDialog(ov, k)
        d2.show()
        pump(3)
        sc_ = d2.findChild(QScrollArea)
        check(f"overlay: the {k} Customize sidebar fits without a horizontal scroll",
              sc_.horizontalScrollBar().maximum() == 0 and sc_.viewport().width() >= d2.form.sizeHint().width())
        d2.preview.repaint()
        d2.close()
    d3 = CustomizeDialog(ov, "full")
    got = []
    ov.on_scene_edited, old_hook = got.append, ov.on_scene_edited
    d3.elements.list_buttons["keyboard"].click()
    d3.elements.widgets["layout"].buttons["half"].click()
    check("overlay: Customize lists the layout's elements and edits them",
          ov.cfg["scene"]["elements"][0]["layout"] == "half" and got and got[-1]["elements"][0]["layout"] == "half")
    ov.on_scene_edited = old_hook
    if old_hook:
        old_hook(ov.cfg["scene"])
    d3.close()
    mv = oc.MOVEMENT_SCHEMA[0][1]
    check("overlay: the movement page offers 'Comet follows' (head by default) and 'invert side button rings'",
          any(o[0] == "center" and o[3] == "head" for o in mv) and any(o[0] == "invert_side_rings" for o in mv))
    ov.split_toggle.setChecked(True)
    ov.flush()
    ids = [e["id"] for e in oc.load()["scene"]["elements"]]
    check("overlay: each element gets its own OBS page", set(ov.element_urls) == set(ids)
          and all("/el/" in ov.element_urls[i].text() for i in ids))
    ov.split_toggle.setChecked(False)
    ov.full_toggle.setChecked(False)
    ov.simple_toggle.setChecked(False)
    ov.replay_toggle.setChecked(False)
    ov.flush()
    check("overlay: everything off again", not oc.helper_wanted(oc.load()))

    # in-app picture from the daemon's stream: real = input color, macro = output color, controller
    vp._stream_live(True)
    now = time.perf_counter()
    vp._stream_event("r", "k", "KEY_Q", 1, 0)
    vp._stream_event("m", "k", "KEY_E", 1, 0)
    vp._stream_event("r", "k", "BTN_SOUTH", 1, 0)
    vp._stream_event("m", "a", "RT", 0.8, 0)
    kbm = vp.area.kbm
    check("visualizer: stream input is split into yours vs macros'",
          "KEY_Q" in kbm.held and "KEY_E" in kbm.out_held and kbm.axis_value("RT") == (0.8, "m"))
    check("visualizer: a controller button suggests adding the controller", "Controller" in vp.pad_hint.text())
    QTest.keyClick(vp.area, Qt.Key_Z)
    check("visualizer: with the stream live, window events don't double-draw keys", "KEY_Z" not in kbm.held
          and "KEY_Z" not in kbm.released)
    vp.area.repaint()
    vp._stream_live(False)
    check("visualizer: falls back to window input when the daemon's gone", not vp.area.stream_live)

    # ---- Edit layout: add / move / resize / remove elements, saved to overlay.json
    vp.edit_btn.setChecked(True)
    pump(3)
    check("edit: the button switches to Done and shows Add / Reset", vp.edit_btn.text() == "Done"
          and vp.add_el_btn.isVisibleTo(vp) and vp.area.editing)
    n0 = len(vp.area.scene["elements"])
    pad = vp.area.add_element("controller")
    for typ in ("mousepad", "joystick"):
        vp.area.add_element(typ)
    ov.flush()
    saved_ids = {e["id"]: e["type"] for e in oc.load()["scene"]["elements"]}
    check("edit: added elements are saved to the scene", len(saved_ids) == n0 + 3
          and pad["id"] in saved_ids and "joystick" in saved_ids.values())
    vp.area.repaint()
    pump(2)
    ox, oy, u = vp.area._geom
    x, y, ew, eh = kl.element_rect(pad)
    start = QPoint(int(ox + (x + ew / 2) * u), int(oy + (y + eh / 2) * u))
    QTest.mousePress(vp.area, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(vp.area, start + QPoint(int(u * 2), int(u)))
    QTest.mouseRelease(vp.area, Qt.LeftButton, Qt.NoModifier, start + QPoint(int(u * 2), int(u)))
    moved = vp.area.element(pad["id"])
    check("edit: dragging moves an element (quarter-key snap)", abs(moved["x"] - (x + 2)) <= 0.25
          and abs(moved["y"] - (y + 1)) <= 0.25 and (moved["x"] * 4) % 1 == 0)
    moved["scale"] = 1.5
    vp.area._scene_changed()
    ov.flush()
    check("edit: resize is saved", next(e for e in oc.load()["scene"]["elements"] if e["id"] == pad["id"])["scale"] == 1.5)
    kb = next(e for e in vp.area.scene["elements"] if e["type"] == "keyboard")
    for preset in kl.KEYBOARD_PRESETS:
        kb["layout"] = preset
        vp.area._scene_changed()
        vp.area.repaint()
    for look in kl.MOUSE_LOOKS:
        vp.area.add_element("mouse")["look"] = look
        vp.area._scene_changed()
        vp.area.repaint()
    check("edit: every keyboard preset and mouse look draws", True)
    # the Edit sidebar: element list + options of the selected one
    check("edit: the sidebar shows while editing and lists every element", vp.panel.isVisibleTo(vp)
          and set(vp.panel.list_buttons) == {e["id"] for e in vp.area.scene["elements"]})
    vp.panel.list_buttons[pad["id"]].click()
    check("edit: picking an element in the sidebar selects it in the picture", vp.area.selected == pad["id"])
    vp.panel.list_buttons["keyboard"].click()
    vp.panel.widgets["layout"].buttons["60"].click()
    ov.flush()
    check("edit: keyboard size toggles (Full / 80% / 60% / Half), saved", vp.area.element("keyboard")["layout"] == "60"
          and next(e for e in oc.load()["scene"]["elements"] if e["id"] == "keyboard")["layout"] == "60")
    vp.panel.list_buttons["mouse"].click()
    vp.panel.widgets["look"].buttons["gaming"].click()
    check("edit: mouse look toggles", vp.area.element("mouse")["look"] == "gaming")
    vp.panel.list_buttons["comet"].click()
    check("edit: a comet follows its head by default", vp.panel.widgets["center"].current() == "head")
    vp.panel.widgets["center"].buttons["tail"].click()
    vp.panel.widgets["invert_side"].setChecked(True)
    check("edit: 'comet follows' and 'invert side button rings' apply", vp.area.element("comet")["center"] == "tail"
          and vp.area.element("comet")["invert_side"] is True)
    vp.panel.widgets["type"].buttons["joystick"].click()
    check("edit: Comet / Mousepad / Joystick toggle switches the view", vp.area.element("comet")["type"] == "joystick"
          and "center" not in vp.panel.widgets)
    vp.panel.widgets["scale"].setValue(1.4)
    check("edit: scale from the sidebar", vp.area.element("comet")["scale"] == 1.4)
    vp.area.selected = None
    vp.area.repaint()
    pump(2)
    gx, gy, gu = vp.area._geom
    px_, py_, pw_, ph_ = kl.element_rect(vp.area.element(pad["id"]))
    at = QPoint(int(gx + (px_ + pw_ / 2) * gu), int(gy + (py_ + ph_ / 2) * gu))
    QTest.mousePress(vp.area, Qt.LeftButton, Qt.NoModifier, at)
    QTest.mouseRelease(vp.area, Qt.LeftButton, Qt.NoModifier, at)
    check("edit: clicking an element in the picture selects it in the sidebar", vp.panel.selected == pad["id"]
          and vp.panel.list_buttons[pad["id"]].isChecked())
    vp.panel.widgets["remove"].click()
    check("edit: remove", vp.area.element(pad["id"]) is None and pad["id"] not in vp.panel.list_buttons)
    vp.area.reset_scene()
    ov.flush()
    check("edit: reset restores the default scene", oc.load()["scene"]["elements"] == kl.DEFAULT_SCENE["elements"])
    vp.edit_btn.setChecked(False)
    check("edit: Done leaves edit mode", not vp.area.editing and vp.edit_btn.text() == "Edit layout")

    L = it.InputLog()
    L.add(0.0, "kd", "KEY_LEFTCTRL"); L.add(0.05, "kd", "KEY_C"); L.add(0.15, "ku", "KEY_C"); L.add(0.2, "ku", "KEY_LEFTCTRL")
    L.add(1.0, "kd", "KEY_A"); L.add(1.08, "ku", "KEY_A")
    L.add(1.5, "wheel", 1); L.add(1.6, "wheel", 1)
    for i in range(50):
        L.add(2.0 + i * 0.001, "move", 2, -1)
    L.add(3.0, "kd", "KEY_W")
    out = L.render()
    check("transcript: pressed-then-released-in-reverse keys become combo()",
          "combo(KEY_LEFTCTRL, KEY_C)" in out)
    check("transcript: a quick press/release becomes tap() with its hold time", "tap(KEY_A, time_=0.08)" in out)
    check("transcript: a wheel run is summed", "wheel(2)" in out)
    check("transcript: a movement burst is one move_mouse()", out.count("move_mouse(") == 1 and "move_mouse(100, -50" in out)
    check("transcript: a key still down stays as kd()", out.rstrip().endswith("kd(KEY_W)"))
    check("transcript: waits between actions", "wait(0.8)" in out)
    check("transcript: no waits when turned off", "wait(" not in L.render(True, False))
    check("transcript: releases of never-seen presses are dropped", it.InputLog().render() == "")
    L2 = it.InputLog()
    L2.add(0.0, "ku", "KEY_Q")
    check("transcript: an orphan release is ignored", L2.render() == "")
    L3 = it.InputLog()
    for i in range(5000):
        L3.add(i * 0.001, "move", 1, 1)
    check("transcript: a 1000Hz burst stays tiny in memory", len(L3.events) < 10)

    # ---- Dictionary page
    import reference as ref
    w.nav.button(app.PAGE_DICTIONARY).click()
    pump(300)
    dp = w.dictionary_page
    ents, _notes = ref.parse_dictionary()
    parsed = {n for e in ents for n in e["names"]}
    check("dictionary: every primitive has an entry", set(ref.primitive_names()) <= parsed)
    check("dictionary: page lists all commands", len(dp.commands.entries) == len(ents))
    check("dictionary: ignore is documented with 'what' (matches the daemon)",
          "ignore(what)" in ref.DICTIONARY_TEXT and "ignore(target)" not in ref.DICTIONARY_TEXT)
    dp.search.setText("wheel")
    pump(50)
    vis = [e.signature for e in dp.commands.entries if not e.isHidden()]
    check("dictionary: search filters commands", vis and all("wheel" in (e.haystack) for e in dp.commands.entries if not e.isHidden())
          and len(vis) < len(ents))
    dp.search.setText("zzzznothingzzzz")
    pump(50)
    check("dictionary: no match shows the empty message and hides sections",
          not dp.empty.isHidden() and dp.commands.isHidden())
    dp.search.setText("")
    pump(50)
    check("dictionary: clearing search shows everything again", dp.empty.isHidden() and not dp.commands.isHidden())
    check("dictionary: colors follow the category scheme",
          ref.PRIMITIVES_BY_NAME["tap"].category == "output" and next(e for e in dp.commands.entries if e.signature.startswith("tap(")).category == "output"
          and next(e for e in dp.commands.entries if e.signature.startswith("waitForPress")).category == "input")
    import dictionary_page as dpm
    first = dp.commands.entries[0]
    check("dictionary: entries start collapsed", first.desc is not None and first.desc.isHidden())
    QTest.mouseClick(first.header, Qt.LeftButton)
    pump(50)
    check("dictionary: clicking a signature expands it", not first.desc.isHidden())
    QTest.mouseClick(first.header, Qt.LeftButton)
    pump(50)
    check("dictionary: clicking again collapses it", first.desc.isHidden())
    check("dictionary: text is 3x the app's size",
          first.header.sig.font().pixelSize() == round(dpm.base_px() * 3) and dpm.TEXT_SCALE == 3)
    dp.search.setText("kernel")                  # only in wait()'s description
    pump(50)
    waite = next(e for e in dp.commands.entries if e.signature.startswith("wait("))
    check("dictionary: a search that matches a description opens that entry",
          not waite.isHidden() and not waite.desc.isHidden())
    dp.search.setText("")
    pump(50)
    check("dictionary: clearing the search closes what it opened", waite.desc.isHidden())
    dp.expand_all(True)
    check("dictionary: expand all", all(not e.desc.isHidden() for e in dp.all_entries() if e.header and e.desc))
    dp.expand_all(False)
    km = dp.keymap
    check("dictionary: key names are drawn on the visualizer's keyboard",
          km.names.get("KEY_A", ("", ""))[1] == "KEY_A" and km.names.get("BTN_LEFT") is not None)
    if km.aliases:
        check("dictionary: the short name is the big label", km.names["KEY_A"][0] == "A"
              and "also:" in km.tooltip_for({"name": "KEY_LEFTCTRL"}))
    dp.search.setText("ctrl")
    pump(50)
    check("dictionary: searching lights matching keys up", "KEY_LEFTCTRL" in km.highlight
          and "KEY_A" not in km.highlight and not dp.keys_title.isHidden())
    dp.search.setText("")
    pump(50)
    check("dictionary: controller commands are documented",
          any(e.signature.startswith("getAxis(") for e in dp.commands.entries)
          and any(e.signature.startswith("axis(") for e in dp.commands.entries))
    ed_btn = [b for b in ed.findChildren(CustomButton) if b.text() == "Open the Dictionary"]
    check("dictionary: the editor has a button in place of the old collapsible panels",
          len(ed_btn) == 1 and not [c for c in ed.findChildren(QLabel) if c.text().startswith("tap(key, time_=0.1)")])
    w.nav.button(app.PAGE_MACROS).click()
    pump(200)

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
