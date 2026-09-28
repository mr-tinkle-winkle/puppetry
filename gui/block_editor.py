"""
The block editor: a palette of blocks on the left, a zoomable/pannable
canvas on the right (QGraphicsView). Drag blocks out of the palette,
snap them together, click a socket to type into it.

Interaction summary:
  * drag a block        -> moves it AND everything below it (Ctrl: just it)
  * drop near a notch   -> snaps (a dashed ghost shows where)
  * drop on the palette -> deletes
  * drag a hat          -> moves the whole script / function
  * click a socket      -> edit it (dropdowns for true/false and choices)
  * drag a reporter (orange/green/red value) OUT of a socket -> move it to
    another socket, or drop it anywhere else to remove it
  * a list socket always has one empty "+" slot at the end: fill it and
    another appears; clear one and it goes away
  * notes: attach them like any block, or drop them anywhere -- a free
    note has no notch and is saved as a `#@note x,y:` line
  * right-click         -> duplicate / delete / else-if / convert to text ...
  * drag empty space    -> pan;  Ctrl+wheel -> zoom;  Ctrl+0 -> reset zoom
  * Ctrl+Z / Ctrl+Shift+Z -> undo / redo;  Delete -> delete selected block

Only the stack under the "when ... pressed" hat is the macro (plus this
macro's functions and notes). Anything else left lying on the canvas is
faded and NOT saved -- the toolbar says so.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QFont, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCompleter, QGraphicsItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

import block_model as bm
import block_render as br
from block_model import Rep
from reference import PRIMITIVES_BY_NAME
from ui_kit.custom_button import CustomButton
from ui_kit.custom_scrollbar import CustomScrollBar
from ui_kit.icons import icon_pixmap
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme, contrast_text
from widgets import ask, dim_label, label_style, prompt_text, section_title

SECTION_ICONS = {"Output": "block_output", "Timing": "block_timing", "Conditions": "block_conditions",
                 "Real input": "block_input", "Variables": "block_variables", "Functions": "block_functions",
                 "My blocks": "block_custom", "Other": "block_other"}

MIME_BLOCK = "application/x-puppetry-block"
MIME_REP = "application/x-puppetry-reporter"

BLOCK_TIPS = {
    "repeat": "Run the blocks inside this many times.",
    "for": "Run the blocks inside once for each item, with the item in a variable "
           "(range(10) = 0, 1, ... 9).",
    "while": "Keep running the blocks inside for as long as the condition is true.",
    "if": "Run the blocks inside only if the condition is true (else if / else: right-click to add).",
    "assign": "Create a variable, or give it a new value.",
    "change": "Add to a variable (or subtract / multiply / divide with the dropdown).",
    "arguments": "This macro's parameters, with their defaults -- other macros and `puppetry --name=... "
                 "values` can pass them. Only goes at the very top.",
    "macro_call": "Run another macro (it finishes before this one carries on).",
    "func_call": "Run one of this macro's own functions.",
    "def": "Your own function, just for this macro. Blocks under it run whenever \"run function\" uses it. "
           "Add names after \"with\" to give it inputs.",
    "return": "Leave the function here (optionally handing back a value). Outside a function: stop this "
              "run of the macro.",
    "comment": "A note. Does nothing. Drop it anywhere on the canvas to keep it free-floating.",
    "raw": "Custom code: any Python you type. Click to edit, right-click to try turning it into blocks.",
    "custom": "A custom block (right-click it in the palette to edit it).",
}
REP_TIPS = {
    "var": "A variable -- drag it into any socket, or out to remove it.",
    "bool": "True or false (click to flip).",
    "compare": "Compares two values: equal to, not equal to, greater than, ...",
    "held": "True while that real key/button is held down right now.",
    "mouse": "The mouse position (x, y) -- or just its x or y (click to choose). Move mouse accepts a saved "
             "position.",
    "buttons": "Every real key/button held right now (a list).",
    "press": "Waits until you press a key/button, then is that key -- e.g. set [button] to [key pressed], "
             "then tap [button].",
}


# ---------------------------------------------------------------------------
# reporter <-> JSON (drag and drop)
# ---------------------------------------------------------------------------

def rep_to_json(v):
    if isinstance(v, Rep):
        return {"rep": v.kind, "name": v.name, "fields": {k: rep_to_json(x) for k, x in v.fields.items()}}
    if isinstance(v, list):
        return [rep_to_json(x) for x in v]
    return v


def rep_from_json(d):
    if isinstance(d, dict) and "rep" in d:
        return Rep(d["rep"], d.get("name", ""), {k: rep_from_json(x) for k, x in (d.get("fields") or {}).items()})
    if isinstance(d, list):
        return [rep_from_json(x) for x in d]
    return d


# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------

def _block_entry(spec, tip):
    return ("block", spec, tip)


def _rep_entry(rep, tip):
    return ("rep", rep, tip)


def palette_sections(ed: "BlockEditor") -> list:
    """[(title, category, [entry])] -- entry: ("block", spec, tip) |
    ("rep", Rep, tip) | ("vars",) | ("button", label, fn, tip)."""
    call = lambda n: _block_entry({"kind": "call", "name": n}, PRIMITIVES_BY_NAME[n].summary)  # noqa: E731
    customs_by_cat: dict = {}
    for d in ed.customs.values():
        customs_by_cat.setdefault(d.category, []).append(
            ("block", {"kind": "custom", "func": d.func}, d.description or f"Custom block: {d.name}"))

    output = [call(n) for n in ("tap", "kd", "ku", "combo", "type", "move_mouse", "wheel", "command")]
    if ed.macro_names:
        output.append(_block_entry({"kind": "macro_call"}, BLOCK_TIPS["macro_call"]))
    sections = [
        ("Output — act on the world", "output", output, "Output"),
        ("Timing & control", "neutral",
         [call("wait"), call("speed"),
          _block_entry({"kind": "repeat"}, BLOCK_TIPS["repeat"]),
          _block_entry({"kind": "for"}, BLOCK_TIPS["for"]),
          _block_entry({"kind": "while"}, BLOCK_TIPS["while"]),
          call("checkpoint"),
          _rep_entry(bm.new_rep("bool", value=True), "True -- drop it into any socket."),
          _rep_entry(bm.new_rep("bool", value=False), "False -- drop it into any socket.")], "Timing & control"),
        ("Conditions", "input",
         [_block_entry({"kind": "if"}, BLOCK_TIPS["if"]),
          _block_entry({"kind": "if", "has_else": True}, "One set of blocks if the condition is true, another if not."),
          _rep_entry(bm.new_rep("compare"), REP_TIPS["compare"]),
          _rep_entry(bm.new_rep("held"), REP_TIPS["held"])], "Conditions"),
        ("Real input", "input",
         [call("waitForPress"), call("waitForReactivation"), call("ignore"), call("ignore_keys"), call("actAs"),
          _rep_entry(bm.new_rep("press"), REP_TIPS["press"]),
          _rep_entry(bm.new_rep("mouse"), REP_TIPS["mouse"]),
          _rep_entry(bm.new_rep("buttons"), REP_TIPS["buttons"])], "Real input"),
        ("Variables", "input",
         [_block_entry({"kind": "arguments"}, BLOCK_TIPS["arguments"]),
          _block_entry({"kind": "assign"}, BLOCK_TIPS["assign"]),
          _block_entry({"kind": "change"}, BLOCK_TIPS["change"]),
          ("vars",)], "Variables"),
        ("Functions (this macro only)", "function",
         [_block_entry({"kind": "def"}, BLOCK_TIPS["def"])]
         + ([_block_entry({"kind": "func_call"}, BLOCK_TIPS["func_call"])] if ed.function_names() else [])
         + [_block_entry({"kind": "return"}, BLOCK_TIPS["return"])], "Functions"),
        ("My blocks", "custom",
         [("button", "+ Create a custom block", ed.create_custom_block,
           "Make your own block from a code template -- usable in every macro.")], "My blocks"),
        ("Other", "neutral",
         [_block_entry({"kind": "comment"}, BLOCK_TIPS["comment"]),
          _block_entry({"kind": "raw"}, "Custom code: type any Python you like.")], "Other"),
    ]
    out = []
    for title, cat, entries, key in sections:
        out.append((title, cat, entries + customs_by_cat.pop(key, [])))
    for cat, entries in customs_by_cat.items():   # categories that no longer exist
        out[-2][2].extend(entries)
    return out


def render_pixmap(lay_blocks, w, h, paint, scale: float = 1.0) -> QPixmap:
    ratio = 2.0
    pm = QPixmap(int(max(10, w * scale) * ratio), int(max(10, h * scale) * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.scale(scale, scale)
    paint(p)
    p.end()
    return pm


def render_blocks_pixmap(ed: "BlockEditor", blocks, scale: float = 1.0) -> QPixmap:
    if blocks and blocks[0].kind == "def":
        lay = ed.layouter().layout_stack([], 0, br.HAT_BUMP + 1, hat=blocks[0])
    else:
        lay = ed.layouter().layout_stack(blocks, 0, 0, allow_top_snap=False)
    top = min((lb.rect.top() for lb in lay.blocks), default=0)

    def paint(p):
        p.translate(1, 1 - top)
        for lb in lay.blocks:
            br.paint_block(p, lb, ed.metrics, ed.theme, customs=ed.customs)
    return render_pixmap(lay, lay.width + 4, lay.height + br.NOTCH_D + 4, paint, scale)


def render_rep_pixmap(ed: "BlockEditor", rep: Rep, scale: float = 1.0) -> QPixmap:
    lb = ed.layouter().layout_reporter(rep)

    def paint(p):
        p.translate(1, 1)
        br.paint_block(p, lb, ed.metrics, ed.theme)
    return render_pixmap(None, lb.w + 3, lb.h + 3, paint, scale)


class _DragSource(QLabel):
    """Common press-and-drag behavior for palette entries."""
    SCALE = 0.8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press: QPoint | None = None
        self.setCursor(Qt.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseReleaseEvent(self, e) -> None:
        self._press = None

    def mime(self) -> QMimeData:
        raise NotImplementedError

    def mouseMoveEvent(self, e) -> None:
        if self._press is None or (e.position().toPoint() - self._press).manhattanLength() < 5:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mime())
        drag.setPixmap(self.pixmap())
        drag.setHotSpot(self._press)
        self._press = None
        drag.exec(Qt.CopyAction)


class PaletteBlock(_DragSource):
    def __init__(self, ed: "BlockEditor", spec: dict, tip: str, parent=None):
        super().__init__(parent)
        self.ed = ed
        self.spec = spec
        blocks = ed.spec_to_blocks(spec, preview=True)
        pm = render_blocks_pixmap(ed, blocks, self.SCALE)
        max_w = 272
        if pm.width() / pm.devicePixelRatio() > max_w:     # shrink wide blocks to fit the palette
            self.scale = self.SCALE * max_w / (pm.width() / pm.devicePixelRatio())
            pm = render_blocks_pixmap(ed, blocks, self.scale)
        else:
            self.scale = self.SCALE
        self.setPixmap(pm)
        self.setToolTip(tip)

    def mime(self) -> QMimeData:
        md = QMimeData()
        hot = self._press
        md.setData(MIME_BLOCK, json.dumps({"spec": self.spec, "hx": hot.x() / self.scale,
                                           "hy": hot.y() / self.scale}).encode())
        return md

    def contextMenuEvent(self, e) -> None:
        if self.spec.get("kind") != "custom":
            return
        func = self.spec.get("func")
        menu = QMenu(self)
        menu.addAction("Edit this custom block…", lambda: self.ed.edit_custom_block(func))
        menu.addAction("Delete this custom block", lambda: self.ed.delete_custom_block(func))
        menu.exec(e.globalPos())


class PaletteReporter(_DragSource):
    def __init__(self, ed: "BlockEditor", rep: Rep, tip: str, parent=None):
        super().__init__(parent)
        self.rep = rep
        self.setPixmap(render_rep_pixmap(ed, rep, self.SCALE))
        self.setToolTip(tip)

    def mime(self) -> QMimeData:
        md = QMimeData()
        md.setData(MIME_REP, json.dumps(rep_to_json(self.rep)).encode())
        return md


class Palette(QWidget):
    def __init__(self, editor: "BlockEditor", parent=None):
        super().__init__(parent)
        self.editor = editor
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(4, 4, 8, 4)
        self._lay.setSpacing(6)
        self._key = None

    def refresh(self, force: bool = False) -> None:
        ed = self.editor
        allv = list(dict.fromkeys(list(ed.variables) + list(ed.extra_vars)))
        key = (tuple(ed.macro_names), tuple(allv), tuple(ed.function_names()),
               tuple((d.func, d.name, d.category, d.color, tuple(a.name for a in d.args)) for d in ed.customs.values()))
        if key == self._key and not force:
            return
        self._key = key
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif it.layout() is not None:
                pass
        theme = ed.theme
        for title, cat, entries in palette_sections(ed):
            head = section_title(title)
            color = theme.category_color(cat)
            head.setStyleSheet(label_style(color, "font-size: 14px; font-weight: bold;"))
            icon_name = next((v for k, v in SECTION_ICONS.items() if title.startswith(k)), None)
            pm = icon_pixmap(icon_name, 18, color) if icon_name else None
            if pm is not None:
                hw = QWidget()
                hl = QHBoxLayout(hw)
                hl.setContentsMargins(0, 0, 0, 0)
                il = QLabel()
                il.setPixmap(pm)
                hl.addWidget(il)
                hl.addWidget(head)
                hl.addStretch(1)
                self._lay.addWidget(hw)
            else:
                self._lay.addWidget(head)
            row_reps: list = []

            def flush_reps():
                if not row_reps:
                    return
                w = QWidget()
                rl = QHBoxLayout(w)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(6)
                for r in row_reps:
                    rl.addWidget(r)
                rl.addStretch(1)
                self._lay.addWidget(w)
                row_reps.clear()

            for entry in entries:
                if entry[0] == "rep":
                    row_reps.append(PaletteReporter(ed, entry[1], entry[2]))
                    if len(row_reps) == 2:
                        flush_reps()
                    continue
                flush_reps()
                if entry[0] == "block":
                    pb = PaletteBlock(ed, entry[1], entry[2])
                    ipm = None
                    if entry[1].get("kind") == "call":
                        prim = PRIMITIVES_BY_NAME.get(entry[1]["name"])
                        ipm = icon_pixmap(f"primitive_{entry[1]['name']}", 18,
                                          theme.category_color(prim.category) if prim else None)
                    if ipm is not None:
                        rw = QWidget()
                        rl = QHBoxLayout(rw)
                        rl.setContentsMargins(0, 0, 0, 0)
                        il = QLabel()
                        il.setPixmap(ipm)
                        rl.addWidget(il)
                        rl.addWidget(pb)
                        rl.addStretch(1)
                        self._lay.addWidget(rw)
                    else:
                        self._lay.addWidget(pb)
                elif entry[0] == "button":
                    b = CustomButton(entry[1])
                    b.setToolTip(entry[3])
                    b.clicked.connect(entry[2])
                    self._wrap(b)
                elif entry[0] == "vars":
                    chips = [PaletteReporter(ed, Rep("var", n), REP_TIPS["var"]) for n in allv[:16]]
                    for i in range(0, len(chips), 3):
                        w = QWidget()
                        rl = QHBoxLayout(w)
                        rl.setContentsMargins(0, 0, 0, 0)
                        rl.setSpacing(6)
                        for c in chips[i:i + 3]:
                            rl.addWidget(c)
                        rl.addStretch(1)
                        self._lay.addWidget(w)
                    nb = CustomButton("+ New variable")
                    nb.setToolTip("Make a variable name to drag into sockets (set it with a \"set\" block).")
                    nb.clicked.connect(ed.new_variable)
                    self._wrap(nb)
            flush_reps()
        self._lay.addWidget(dim_label("Drag blocks onto the canvas. Drop a block back here to delete it. "
                                      "Drag orange/green/red values into sockets -- and out of them."))
        self._lay.addStretch(1)

    def _wrap(self, button) -> None:
        w = QWidget()
        r = QHBoxLayout(w)
        r.setContentsMargins(0, 0, 0, 0)
        r.addWidget(button)
        r.addStretch(1)
        self._lay.addWidget(w)


# ---------------------------------------------------------------------------
# canvas items
# ---------------------------------------------------------------------------

class StackItem(QGraphicsItem):
    """One stack of blocks: the main script ("main", under the hat), one of
    this macro's functions ("def", under its create-function hat), or a
    loose/dragged stack ("loose")."""

    def __init__(self, editor: "BlockEditor", role: str, blocks: list, owner=None):
        super().__init__()
        self.editor = editor
        self.role = role
        self.blocks = blocks
        self.owner = owner   # def Block / bm.Stack / None
        self.layout: br.StackLayout | None = None
        self.setFlag(QGraphicsItem.ItemUsesExtendedStyleOption, True)
        self.relayout()

    @property
    def stack(self):
        return self.owner if isinstance(self.owner, bm.Stack) else None

    def relayout(self) -> None:
        self.prepareGeometryChange()
        lay = self.editor.layouter()
        hat = self.editor.hat_text if self.role == "main" else (self.owner if self.role == "def" else None)
        self.layout = lay.layout_stack(self.blocks, 0, 0, hat=hat, allow_top_snap=self.role == "loose")
        if self.role == "loose":
            self.setOpacity(1.0 if self.editor.dragging_item is self else 0.6)

    def boundingRect(self) -> QRectF:
        lay = self.layout
        return QRectF(-4, -br.HAT_BUMP - 8, (lay.width if lay else 0) + 60, (lay.height if lay else 0) + br.HAT_BUMP + 24)

    def paint(self, p: QPainter, option, widget=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        ed = self.editor
        drop = ed.drop_target
        for lb in self.layout.visible(option.exposedRect):
            br.paint_block(p, lb, ed.metrics, ed.theme, selected=(lb.block is ed.selected),
                           drop_ref=(drop[1] if drop and drop[0] is lb.block else None), customs=ed.customs)

    def hit(self, pos: QPointF):
        """(LaidBlock, part, FieldHit|None), topmost first. part: field | toggle | block."""
        r = QRectF(pos.x(), pos.y(), 1, 1)
        for lb in reversed(self.layout.visible(r)):
            if not lb.rect.contains(pos):
                continue
            if lb.toggle is not None and lb.toggle.contains(pos):
                return lb, "toggle", None
            for fh in reversed(lb.fields):
                if fh.rect.contains(pos):
                    return lb, "field", fh
            if lb.path().contains(pos):
                return lb, "block", None
        return None


class NoteItem(QGraphicsItem):
    """A free-floating note -- a plain card, no notch: it's attached to nothing."""

    def __init__(self, editor: "BlockEditor", note: bm.Note):
        super().__init__()
        self.editor = editor
        self.note = note
        self.setPos(note.x, note.y)
        self.setZValue(5)

    def size(self):
        return br.note_size(self.note.text, self.editor.metrics)

    def boundingRect(self) -> QRectF:
        w, h = self.size()
        return QRectF(-2, -2, w + 4, h + 4)

    def paint(self, p: QPainter, option, widget=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.size()
        br.paint_note(p, QRectF(0, 0, w, h), self.note.text, self.editor.metrics, self.editor.theme,
                      selected=self.editor.selected is self.note)

    def hit(self, pos: QPointF) -> bool:
        w, h = self.size()
        return QRectF(0, 0, w, h).contains(pos)


class GhostItem(QGraphicsItem):
    """The snap preview. Sits above every stack but below the block being
    dragged (z 500 vs 1000), so the dragged block stays readable."""

    def __init__(self, editor: "BlockEditor"):
        super().__init__()
        self.editor = editor
        self.setZValue(500)
        self._rect = QRectF()

    def sync(self) -> None:
        g = self.editor.ghost
        r = QRectF() if g is None else QRectF(g[0] - 4, (g[1] - g[3] if g[4] else g[1]) - 4, g[2] + 8,
                                              g[3] + br.NOTCH_D + 8)
        if r != self._rect:
            self.prepareGeometryChange()
            self._rect = r
        self.update()

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, p: QPainter, option, widget=None) -> None:
        g = self.editor.ghost
        if g is not None:
            p.setRenderHint(QPainter.Antialiasing)
            br.paint_ghost(p, g[0], g[1], g[2], g[3], self.editor.theme, bottom=g[4])


class RepDragItem(QGraphicsItem):
    """A reporter being dragged around the canvas (out of a socket)."""

    def __init__(self, editor: "BlockEditor", rep: Rep):
        super().__init__()
        self.editor = editor
        self.rep = rep
        self.lb = editor.layouter().layout_reporter(rep)
        self.setZValue(1000)

    def boundingRect(self) -> QRectF:
        return QRectF(-2, -2, self.lb.w + 4, self.lb.h + 4)

    def paint(self, p: QPainter, option, widget=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        br.paint_block(p, self.lb, self.editor.metrics, self.editor.theme)


@dataclass
class Hit:
    item: object
    lb: "br.LaidBlock | None" = None
    fh: "br.FieldHit | None" = None
    part: str = "block"        # block | field | toggle | note

    @property
    def block(self):
        return self.lb.block if self.lb is not None else None


class BlockView(QGraphicsView):
    def __init__(self, editor: "BlockEditor"):
        super().__init__(editor.scene)
        self.editor = editor
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setVerticalScrollBar(CustomScrollBar(Qt.Vertical))
        self.setHorizontalScrollBar(CustomScrollBar(Qt.Horizontal))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.zoom = 1.0
        self._press = None      # (scene_pos, view_pos, Hit, modifiers)
        self._pan = None
        self._tip = ""

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.editor.update_scene_rect()

    # -- zoom --------------------------------------------------------------
    def set_zoom(self, z: float) -> None:
        z = max(0.35, min(2.5, z))
        f = z / self.zoom
        self.zoom = z
        self.scale(f, f)
        self.editor.close_field_editor(commit=True)
        self.editor.zoom_changed()
        self.editor.update_scene_rect()

    def wheelEvent(self, e) -> None:
        if e.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.12 if e.angleDelta().y() > 0 else 1 / 1.12))
            e.accept()
            return
        self.editor.close_field_editor(commit=True)
        super().wheelEvent(e)

    # -- mouse ---------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        ed = self.editor
        ed.close_field_editor(commit=True)
        self.setFocus()
        sp = self.mapToScene(e.position().toPoint())
        hit = ed.hit_test(sp)
        if e.button() == Qt.RightButton:
            if hit:
                ed.select_hit(hit)
                ed.context_menu(hit, e.globalPosition().toPoint())
            return
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and hit is None):
            ed.select(None)
            self._pan = (e.position().toPoint(), self.horizontalScrollBar().value(), self.verticalScrollBar().value())
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            self._press = (sp, e.position().toPoint(), hit, e.modifiers())
            ed.select_hit(hit)

    def mouseMoveEvent(self, e) -> None:
        ed = self.editor
        if self._pan is not None:
            start, hx, vy = self._pan
            d = e.position().toPoint() - start
            self.horizontalScrollBar().setValue(hx - d.x())
            self.verticalScrollBar().setValue(vy - d.y())
            return
        sp = self.mapToScene(e.position().toPoint())
        if ed.drag is not None:
            ed.drag_move(sp)
            self._autoscroll(e.position().toPoint())
            return
        if self._press is not None:
            if (e.position().toPoint() - self._press[1]).manhattanLength() > 6:
                psp, _vp, hit, mods = self._press
                self._press = None
                ed.begin_drag(hit, psp, single=bool(mods & Qt.ControlModifier))
                if ed.drag is not None:
                    ed.drag_move(sp)
            return
        hit = ed.hit_test(sp)
        cur = Qt.ArrowCursor
        if hit:
            if hit.part == "toggle":
                cur = Qt.PointingHandCursor
            elif hit.fh is not None and hit.fh.rep is None and hit.fh.kind != "cmpop":
                cur = Qt.IBeamCursor if hit.fh.kind in ("text", "name", "code", "note", "append") else Qt.PointingHandCursor
            else:
                cur = Qt.OpenHandCursor
        self.viewport().setCursor(cur)
        tip = ed.tip_for(hit) if hit else ""
        if tip != self._tip:
            self._tip = tip
            if tip:
                QToolTip.showText(e.globalPosition().toPoint(), tip, self.viewport())
            else:
                QToolTip.hideText()

    def _autoscroll(self, vp: QPoint) -> None:
        m = 30
        r = self.viewport().rect()
        dx = -12 if vp.x() < m else (12 if vp.x() > r.width() - m else 0)
        dy = -12 if vp.y() < m else (12 if vp.y() > r.height() - m else 0)
        if dx:
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + dx)
        if dy:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + dy)

    def mouseReleaseEvent(self, e) -> None:
        ed = self.editor
        if self._pan is not None:
            self._pan = None
            self.viewport().setCursor(Qt.ArrowCursor)
            return
        if ed.drag is not None:
            ed.end_drag(self.mapToScene(e.position().toPoint()))
            return
        if self._press is not None:
            _sp, _vp, hit, _m = self._press
            self._press = None
            if hit:
                ed.click(hit)

    def mouseDoubleClickEvent(self, e) -> None:
        self.mousePressEvent(e)

    def keyPressEvent(self, e) -> None:
        ed = self.editor
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and ed.selected is not None:
            ed.delete_selected()
            return
        if e.modifiers() & Qt.ControlModifier and e.key() == Qt.Key_0:
            self.set_zoom(1.0)
            return
        super().keyPressEvent(e)

    # -- drops from the palette ---------------------------------------------
    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasFormat(MIME_BLOCK) or md.hasFormat(MIME_REP):
            e.acceptProposedAction()
            self.editor.close_field_editor(commit=True)
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        md = e.mimeData()
        sp = self.mapToScene(e.position().toPoint())
        if md.hasFormat(MIME_BLOCK):
            info = json.loads(bytes(md.data(MIME_BLOCK)).decode())
            self.editor.external_drag_move(info, sp)
            e.acceptProposedAction()
        elif md.hasFormat(MIME_REP):
            ok = self.editor.rep_hover(sp)
            e.setAccepted(ok)
            if ok:
                e.acceptProposedAction()
        self._autoscroll(e.position().toPoint())

    def dragLeaveEvent(self, e) -> None:
        self.editor.clear_hover_state()

    def dropEvent(self, e) -> None:
        md = e.mimeData()
        sp = self.mapToScene(e.position().toPoint())
        if md.hasFormat(MIME_BLOCK):
            info = json.loads(bytes(md.data(MIME_BLOCK)).decode())
            self.editor.external_drop(info, sp)
            e.acceptProposedAction()
        elif md.hasFormat(MIME_REP):
            rep = rep_from_json(json.loads(bytes(md.data(MIME_REP)).decode()))
            self.editor.rep_drop(rep, sp)
            e.acceptProposedAction()

    def drawBackground(self, p: QPainter, rect: QRectF) -> None:
        bg = self.editor.theme.page_background()
        p.fillRect(rect, bg)
        if self.zoom < 0.5:
            return
        dot = QColor(self.editor.theme.text())
        dot.setAlpha(28)
        step = 24
        p.setPen(QPen(dot, 2.0 / max(self.zoom, 0.01)))
        x0 = int(rect.left() // step) * step
        y0 = int(rect.top() // step) * step
        pts = []
        y = y0
        while y < rect.bottom():
            x = x0
            while x < rect.right():
                pts.append(QPointF(x, y))
                x += step
            y += step
        p.drawPoints(pts)


# ---------------------------------------------------------------------------
# the editor
# ---------------------------------------------------------------------------

class BlockEditor(QWidget):
    changed = Signal()          # the blocks were edited by the user
    customs_changed = Signal()  # the custom block library was edited (saved to disk)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = Theme()
        self.metrics = br.Metrics()
        self.doc = bm.Doc()
        self.hat_text = "when this macro runs"
        self.macro_names: list[str] = []
        self.key_names: list[str] = []
        self.customs: dict = {}
        self.extra_vars: list[str] = []
        self.variables: list[str] = []
        self.selected = None        # a Block or a Note
        self.dragging_item = None
        self.drag = None            # ("stack"|"hat"|"note"|"rep", item, extra)
        self._drag_offset = QPointF()
        self._drag_from_palette = None
        self.snap: br.SnapTarget | None = None
        self._ghost = None
        self.drop_target = None     # (block, ref) highlighted socket
        self._undo: list = []
        self._redo: list = []
        self.revision = 0
        self._field_editor = None
        self._items: list = []
        self.main_item = None
        self.custom_dialog_factory = None   # set by the page (custom_block_dialog.CustomBlockDialog)

        self.scene = QGraphicsScene(self)
        self.ghost_item = GhostItem(self)
        self.scene.addItem(self.ghost_item)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.palette_widget = Palette(self)
        pscroll = SmoothScrollArea()
        pscroll.setWidgetResizable(True)
        pscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        pscroll.setWidget(self.palette_widget)
        pscroll.setFixedWidth(300)
        pscroll.setAutoFillBackground(False)
        pscroll.viewport().setAutoFillBackground(False)
        pscroll.setAcceptDrops(False)
        self.palette_scroll = pscroll
        root.addWidget(pscroll)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.loose_label = QLabel("")
        self.loose_label.setWordWrap(True)
        self.loose_label.setStyleSheet(label_style(self.theme.input_color()))
        tools.addWidget(self.loose_label, stretch=1)
        self.undo_btn = CustomButton("↶")
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn = CustomButton("↷")
        self.redo_btn.setToolTip("Redo (Ctrl+Shift+Z)")
        self.redo_btn.clicked.connect(self.redo)
        self.tidy_btn = CustomButton("Tidy up")
        self.tidy_btn.setToolTip("Line up functions and loose stacks to the right of the macro")
        self.tidy_btn.clicked.connect(self.tidy)
        self.zoom_label = CustomButton("100%")
        self.zoom_label.setToolTip("Reset zoom (Ctrl+0). Ctrl+scroll to zoom.")
        self.zoom_label.clicked.connect(lambda: self.view.set_zoom(1.0))
        for b in (self.undo_btn, self.redo_btn, self.tidy_btn, self.zoom_label):
            tools.addWidget(b)
        right.addLayout(tools)
        self.view = BlockView(self)
        right.addWidget(self.view, stretch=1)
        root.addLayout(right, stretch=1)

        for seq, fn in ((QKeySequence("Ctrl+Z"), self.undo), (QKeySequence("Ctrl+Shift+Z"), self.redo),
                        (QKeySequence("Ctrl+Y"), self.redo)):
            sc = QShortcut(seq, self.view)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

        self._rebuild()

    # ------------------------------------------------------------------ public API

    def layouter(self) -> br.Layouter:
        return br.Layouter(self.metrics, self.variables, self.customs)

    def function_names(self) -> list[str]:
        return bm.function_names(self.doc)

    def set_doc(self, doc: bm.Doc, reset_view: bool = True, keep_undo: bool = False) -> None:
        self.close_field_editor(commit=False)
        self.doc = doc
        self.selected = None
        if not keep_undo:
            self._undo.clear()
            self._redo.clear()
        self._place_functions()
        self._rebuild()
        if reset_view:
            self.view.horizontalScrollBar().setValue(self.view.horizontalScrollBar().minimum())
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().minimum())

    def code(self) -> str:
        self.close_field_editor(commit=True)
        return bm.blocks_to_code(self.doc, customs=self.customs)

    def set_context(self, macro_names=None, hat_text=None, key_names=None, customs=None) -> None:
        relayout = False
        if macro_names is not None:
            self.macro_names = list(macro_names)
        if key_names is not None:
            self.key_names = list(key_names)
        if customs is not None:
            self.customs = dict(customs)
            relayout = True
        if hat_text is not None and hat_text != self.hat_text:
            self.hat_text = hat_text
            relayout = True
        if relayout:
            self._rebuild()
        self.palette_widget.refresh()

    def loose_count(self) -> int:
        return sum(len(list(bm.walk(s.blocks))) for s in self.doc.loose)

    def block_count(self) -> int:
        return len(list(bm.walk_doc(self.doc)))

    def begin_external_edit(self) -> None:
        """Before something outside the canvas rewrites the blocks (e.g. a
        transcription): one undo step covers the whole thing."""
        self.push_undo()
        self.undo_btn.setEnabled(True)

    # ------------------------------------------------------------------ build

    def _place_functions(self) -> None:
        """Functions parsed from code have no position yet: stack them to the
        right of the main script."""
        lay = self.layouter().layout_stack(self.doc.main, 0, 0, hat=self.hat_text)
        x = self.doc.main_x + max(lay.width, 200) + 80
        y = self.doc.main_y
        for f in self.doc.functions:
            if f.x == 0 and f.y == 0:
                f.x, f.y = x, y
                fl = self.layouter().layout_stack(f.bodies[0], 0, 0, hat=f)
                y += fl.height + 50

    def _rebuild(self) -> None:
        self.variables = []
        for v in bm.variable_names(self.doc.main) + bm.variable_names(self.doc.functions) + \
                [v for s in self.doc.loose for v in bm.variable_names(s.blocks)]:
            if v not in self.variables:
                self.variables.append(v)
        keep = self.dragging_item
        for it in self._items:
            if it is not keep and it.scene() is self.scene:
                self.scene.removeItem(it)
        self._items = []
        main = StackItem(self, "main", self.doc.main)
        main.setPos(self.doc.main_x, self.doc.main_y)
        self.scene.addItem(main)
        self._items.append(main)
        self.main_item = main
        for f in self.doc.functions:
            if not f.bodies:
                f.bodies = [[]]
            it = StackItem(self, "def", f.bodies[0], f)
            it.setPos(f.x, f.y)
            self.scene.addItem(it)
            self._items.append(it)
        for st in self.doc.loose:
            it = StackItem(self, "loose", st.blocks, st)
            it.setPos(st.x, st.y)
            self.scene.addItem(it)
            self._items.append(it)
        for n in self.doc.notes:
            it = NoteItem(self, n)
            self.scene.addItem(it)
            self._items.append(it)
        if keep is not None and keep not in self._items:
            self._items.append(keep)
        self.update_scene_rect()
        n = self.loose_count()
        self.loose_label.setText(
            f"{n} loose block{'s' if n != 1 else ''} not attached under a hat -- "
            "not part of the macro, won't be saved." if n else "")
        self.undo_btn.setEnabled(bool(self._undo))
        self.redo_btn.setEnabled(bool(self._redo))
        self.palette_widget.refresh()
        self.scene.update()

    def update_scene_rect(self) -> None:
        """Just the blocks (plus a margin) -- at least the visible area, so the
        scroll bars only appear when blocks actually reach past the edges."""
        r = QRectF()
        for it in self._items:
            r = r.united(it.sceneBoundingRect())
        if r.isNull():
            r = QRectF(0, 0, 1, 1)
        r = r.adjusted(-30, -30, 60, 60)
        vp = self.view.viewport().size() if hasattr(self, "view") else None
        if vp is not None:
            z = self.view.zoom or 1.0
            base = QRectF(min(r.left(), 0), min(r.top(), 0), vp.width() / z, vp.height() / z)
            r = r.united(base)
        self.scene.setSceneRect(r)

    def zoom_changed(self) -> None:
        self.zoom_label.setText(f"{round(self.view.zoom * 100)}%")

    # ------------------------------------------------------------------ undo

    def _snapshot(self):
        d = self.doc
        return (bm.copy_blocks(d.main), bm.copy_blocks(d.functions),
                [bm.Note(n.text, n.x, n.y, n.uid) for n in d.notes],
                [bm.Stack(bm.copy_blocks(st.blocks), st.x, st.y) for st in d.loose], d.main_x, d.main_y)

    def push_undo(self) -> None:
        self._undo.append(self._snapshot())
        if len(self._undo) > 80:
            self._undo.pop(0)
        self._redo.clear()

    def _restore(self, snap) -> None:
        main, functions, notes, loose, mx, my = snap
        self.doc.main[:] = main
        self.doc.functions = functions
        self.doc.notes = notes
        self.doc.loose = loose
        self.doc.main_x, self.doc.main_y = mx, my

    def undo(self) -> None:
        self.close_field_editor(commit=True)
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self.selected = None
        self._mutated()

    def redo(self) -> None:
        self.close_field_editor(commit=True)
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self.selected = None
        self._mutated()

    def _mutated(self) -> None:
        self.revision += 1
        self._rebuild()
        self.changed.emit()

    # ------------------------------------------------------------------ hit testing / selection

    def hit_test(self, sp: QPointF) -> "Hit | None":
        for it in reversed(sorted(self._items, key=lambda i: i.zValue())):
            if it is self.dragging_item:
                continue
            local = it.mapFromScene(sp)
            if isinstance(it, NoteItem):
                if it.hit(local):
                    return Hit(it, part="note")
                continue
            h = it.hit(local)
            if h:
                lb, part, fh = h
                return Hit(it, lb, fh, part)
        return None

    def select(self, obj) -> None:
        if obj is not self.selected:
            self.selected = obj
            self.scene.update()

    def select_hit(self, hit: "Hit | None") -> None:
        if hit is None:
            self.select(None)
        elif hit.part == "note":
            self.select(hit.item.note)
        elif hit.lb is not None and hit.lb.hat != "main":
            self.select(hit.lb.block)
        else:
            self.select(None)

    def tip_for(self, hit: Hit) -> str:
        if hit.part == "note":
            return "A free-floating note (attached to nothing). Drag it into a stack to attach it."
        if hit.part == "toggle":
            return "Show / hide this block's optional settings"
        lb, fh = hit.lb, hit.fh
        if lb.hat == "main":
            return ("Everything snapped under this runs when the macro is triggered. Drag the hat to move "
                    "the whole script.")
        b = lb.block
        if fh is not None:
            if fh.rep is not None and fh.kind != "cmpop":
                return REP_TIPS.get(fh.kind, "")
            if fh.kind == "cmpop":
                return "How to compare the two sides."
            if fh.kind == "append":
                return "Add another one (an empty slot always waits at the end)."
            p = br.param_default(b, fh.key)
            if p is not None and p.doc:
                return p.doc + (f"  (default: {p.default})" if p.default is not None else "")
            if b.kind == "custom":
                d = self.customs.get(b.name)
                a = next((a for a in d.args if a.name == fh.key), None) if d else None
                if a:
                    return f"{a.name} (default: {a.default})"
        if b.kind == "call" and b.name in PRIMITIVES_BY_NAME:
            return PRIMITIVES_BY_NAME[b.name].summary
        if b.kind == "custom":
            d = self.customs.get(b.name)
            return (d.description if d and d.description else f"Custom block ({b.name}) -- edit it by "
                    "right-clicking it in the palette.")
        return BLOCK_TIPS.get(b.kind, "")

    # ------------------------------------------------------------------ specs -> blocks

    def _unique_function_name(self) -> str:
        names = set(self.function_names())
        n = 1
        while f"my_function{'' if n == 1 else n}" in names:
            n += 1
        return f"my_function{'' if n == 1 else n}"

    def spec_to_blocks(self, spec: dict, preview: bool = False) -> list:
        kind = spec.get("kind")
        if kind == "call":
            return [bm.new_call(spec["name"])]
        if kind == "macro_call":
            return [bm.new_block("macro_call", name=spec.get("name") or (self.macro_names[0] if self.macro_names else ""))]
        if kind == "func_call":
            fn = self.function_names()
            return [bm.new_block("func_call", name=spec.get("name") or (fn[0] if fn else ""))]
        if kind == "def":
            return [bm.new_block("def", name="my_function" if preview else self._unique_function_name())]
        if kind == "custom":
            d = self.customs.get(spec.get("func"))
            if d is None:
                return [bm.new_block("raw", code="# (missing custom block)")]
            return [bm.new_block("custom", defn=d)]
        if kind in ("assign", "change"):
            v = self.variables[0] if self.variables else "x"
            return [bm.new_block(kind, var=v)]
        kw = {k: v for k, v in spec.items() if k != "kind"}
        return [bm.new_block(kind, **kw)]

    # ------------------------------------------------------------------ dragging

    @property
    def ghost(self):
        return self._ghost

    @ghost.setter
    def ghost(self, value) -> None:
        self._ghost = value
        if hasattr(self, "ghost_item"):
            self.ghost_item.sync()

    def begin_drag(self, hit: Hit, press_sp: QPointF, single: bool = False) -> None:
        if hit.part == "note":
            self.push_undo()
            self.drag = ("note", hit.item, None)
            self.dragging_item = hit.item
            hit.item.setZValue(1000)
            self._drag_offset = press_sp - hit.item.pos()
            return
        lb, fh = hit.lb, hit.fh
        # a reporter dragged OUT of its socket
        if fh is not None and fh.rep is not None and fh.kind != "cmpop":
            self.push_undo()
            rep = bm.get_socket(lb.block, fh.ref)
            if not isinstance(rep, Rep):
                return
            bm.set_socket(lb.block, fh.ref, "")
            item = RepDragItem(self, rep)
            self.scene.addItem(item)
            item.setPos(press_sp - QPointF(10, item.lb.h / 2))
            self.drag = ("rep", item, rep)
            self.dragging_item = item
            self._drag_offset = QPointF(10, item.lb.h / 2)
            self.revision += 1
            self._rebuild()
            return
        if lb.hat:
            self.push_undo()
            self.drag = ("hat", hit.item, None)
            self.dragging_item = hit.item
            hit.item.setZValue(1000)
            self._drag_offset = press_sp - hit.item.pos()
            return
        self.push_undo()
        it = hit.item
        container, idx = lb.container, lb.index
        origin = it.mapToScene(QPointF(lb.x, lb.y))
        if single:
            moved = [container.pop(idx)]
        else:
            moved = container[idx:]
            del container[idx:]
        if it.stack is not None and not it.stack.blocks:
            self.doc.loose.remove(it.stack)
        st = bm.Stack(moved, origin.x(), origin.y())
        item = StackItem(self, "loose", st.blocks, st)
        item.setPos(origin)
        item.setZValue(1000)
        self.scene.addItem(item)
        self.dragging_item = item
        self.drag = ("stack", item, None)
        item.relayout()
        self._drag_offset = press_sp - origin
        self._rebuild()

    def drag_move(self, sp: QPointF) -> None:
        kind, item, extra = self.drag
        item.setPos(sp - self._drag_offset)
        if kind == "hat":
            self._sync_hat_pos(item)
            return
        if kind == "rep":
            self.rep_hover(sp)
            return
        if kind == "note":
            blocks = [bm.new_block("comment")]
            self._find_snap(item.pos(), blocks, br.MIN_H)
        else:
            self._find_snap(item.pos(), item.blocks, item.layout.height)
        self.view.viewport().update()

    def _sync_hat_pos(self, item) -> None:
        if item.role == "main":
            self.doc.main_x, self.doc.main_y = item.pos().x(), item.pos().y()
        elif item.role == "def":
            item.owner.x, item.owner.y = item.pos().x(), item.pos().y()

    def _find_snap(self, top_left: QPointF, blocks: list, height: float) -> None:
        best, best_d = None, br.SNAP_DIST
        first = blocks[0] if blocks else None
        dragged_args = first is not None and first.kind == "arguments"
        main_has_args = bool(self.doc.main) and self.doc.main[0].kind == "arguments"
        bottom_left = QPointF(top_left.x(), top_left.y() + height)
        for item in self._items:
            if item is self.dragging_item or not isinstance(item, StackItem):
                continue
            ox, oy = item.pos().x(), item.pos().y()
            for s in item.layout.snaps:
                ref = bottom_left if s.kind == "top" else top_left
                if s.kind == "top" and dragged_args:
                    continue
                d = ((s.x + ox - ref.x()) ** 2 + (s.y + oy - ref.y()) ** 2) ** 0.5
                if d >= best_d:
                    continue
                is_main_top = s.container is self.doc.main and s.index == 0
                if dragged_args and not (is_main_top and not main_has_args):
                    continue
                if not dragged_args and is_main_top and main_has_args:
                    continue
                if s.kind == "top" and s.container and s.container[0].kind == "arguments":
                    continue
                best, best_d = (s, ox, oy), d
        if best is None:
            self.snap, self.ghost = None, None
            return
        s, ox, oy = best
        self.snap = s
        lay = self.layouter().layout_stack(blocks[:1], 0, 0, allow_top_snap=False)
        w = lay.blocks[0].w if lay.blocks else 120
        h = lay.blocks[0].h if lay.blocks else br.MIN_H
        if s.kind == "top":
            self.ghost = (s.x + ox, s.y + oy, w, br.MIN_H, True)
        else:
            self.ghost = (s.x + ox, s.y + oy, w, min(h, 60.0), False)

    def _over_palette(self) -> bool:
        pw = self.palette_scroll
        return pw.rect().contains(pw.mapFromGlobal(QCursor.pos()))

    def end_drag(self, sp: QPointF) -> None:
        kind, item, extra = self.drag
        self.drag = None
        self.dragging_item = None
        snap, self.snap, self.ghost = self.snap, None, None
        if kind == "hat":
            item.setZValue(0)
            self._sync_hat_pos(item)
            if item.role == "def" and self._over_palette():
                self.doc.functions = [f for f in self.doc.functions if f is not item.owner]
            self._mutated()
            return
        if kind == "rep":
            target = self.drop_target
            self.scene.removeItem(item)
            self.drop_target = None
            if target is not None and not self._over_palette():
                block, ref = target
                self._drop_rep_value(block, ref, extra)
            self._mutated()
            return
        if kind == "note":
            item.setZValue(5)
            note = item.note
            if self._over_palette():
                self.doc.notes = [n for n in self.doc.notes if n is not note]
            elif snap is not None:
                self.doc.notes = [n for n in self.doc.notes if n is not note]
                cb = bm.new_block("comment")
                cb.fields["text"] = " " + note.text
                self._insert(snap, [cb], item.pos())
            else:
                note.x, note.y = item.pos().x(), item.pos().y()
            self._mutated()
            return
        # a stack of blocks
        self.scene.removeItem(item)
        blocks = item.blocks
        if self._over_palette():
            if self.selected is not None and any(self.selected is b for b in bm.walk(blocks)):
                self.selected = None
        elif snap is not None:
            self._insert(snap, blocks, item.pos())
        elif len(blocks) == 1 and blocks[0].kind == "comment":
            # a lone note dropped on empty canvas: free-floating (and saved)
            self.doc.notes.append(bm.Note(blocks[0].fields.get("text", "").strip(), item.pos().x(), item.pos().y()))
        else:
            self.doc.loose.append(bm.Stack(blocks, item.pos().x(), item.pos().y()))
        self._mutated()
        self.view.viewport().update()

    def _insert(self, snap: br.SnapTarget, blocks: list, pos: QPointF) -> None:
        if snap.kind == "top":
            snap.container[0:0] = blocks
            owner = next((s for s in self.doc.loose if s.blocks is snap.container), None)
            if owner is not None:
                owner.x, owner.y = pos.x(), pos.y()
        else:
            snap.container[snap.index:snap.index] = blocks

    # -- palette drags (blocks) ----------------------------------------------
    def external_drag_move(self, info: dict, sp: QPointF) -> None:
        spec = info["spec"]
        if spec.get("kind") == "def":
            self.snap, self.ghost = None, None
            return
        if self._drag_from_palette is None or self._drag_from_palette[0] != spec:
            self._drag_from_palette = (spec, self.spec_to_blocks(spec, preview=True))
        blocks = self._drag_from_palette[1]
        top_left = QPointF(sp.x() - info.get("hx", 10), sp.y() - info.get("hy", 10))
        lay = self.layouter().layout_stack(blocks, 0, 0, allow_top_snap=False)
        self._find_snap(top_left, blocks, lay.height)
        self.view.viewport().update()

    def external_drop(self, info: dict, sp: QPointF) -> None:
        spec = info["spec"]
        top_left = QPointF(sp.x() - info.get("hx", 10), sp.y() - info.get("hy", 10))
        blocks = self.spec_to_blocks(spec)
        self.push_undo()
        if spec.get("kind") == "def":
            f = blocks[0]
            f.x, f.y = top_left.x(), top_left.y() + br.HAT_BUMP
            self.doc.functions.append(f)
            self.clear_hover_state()
            self.selected = f
            self._mutated()
            return
        lay = self.layouter().layout_stack(blocks, 0, 0, allow_top_snap=False)
        self._find_snap(top_left, blocks, lay.height)
        snap = self.snap
        self.clear_hover_state()
        if snap is not None:
            self._insert(snap, blocks, top_left)
        elif spec.get("kind") == "comment":
            self.doc.notes.append(bm.Note("note", top_left.x(), top_left.y()))
        else:
            self.doc.loose.append(bm.Stack(blocks, top_left.x(), top_left.y()))
        self.selected = blocks[0]
        self._mutated()

    def add_blocks(self, blocks: list, snap: "br.SnapTarget | None" = None) -> None:
        self.push_undo()
        if snap is None:
            self.doc.main.extend(blocks)
        else:
            self._insert(snap, blocks, QPointF())
        self._mutated()

    def clear_hover_state(self) -> None:
        self.snap, self.ghost, self.drop_target = None, None, None
        self._drag_from_palette = None
        self.view.viewport().update()
        self.scene.update()

    # -- reporters -------------------------------------------------------------
    def rep_hover(self, sp: QPointF) -> bool:
        hit = self.hit_test(sp)
        target = None
        if hit and hit.fh is not None and hit.fh.accepts and hit.fh.kind not in ("cmpop",):
            target = (hit.lb.block, hit.fh.ref)
        if target != self.drop_target:
            self.drop_target = target
            self.scene.update()
        return target is not None

    def _drop_rep_value(self, block, ref, rep) -> None:
        bm.set_socket(block, ref, rep)

    def rep_drop(self, rep: Rep, sp: QPointF) -> None:
        ok = self.rep_hover(sp)
        target = self.drop_target
        self.clear_hover_state()
        if not ok or target is None:
            return
        self.push_undo()
        self._drop_rep_value(target[0], target[1], rep)
        self._mutated()

    def new_variable(self) -> None:
        name = prompt_text(self, "New variable", "")
        if name is None:
            return
        name = name.strip().replace(" ", "_")
        if not name.isidentifier():
            return
        if name not in self.extra_vars:
            self.extra_vars.append(name)
        self.palette_widget.refresh()

    # ------------------------------------------------------------------ edits

    def set_socket(self, block, ref, value) -> None:
        snap = self._snapshot()
        if bm.set_socket(block, ref, value):
            self._undo.append(snap)
            self._redo.clear()
            self._mutated()

    def toggle_expanded(self, block: bm.Block) -> None:
        block.expanded = not block.expanded
        self._rebuild()

    def _find_container(self, block: bm.Block):
        def search(lst):
            for i, b in enumerate(lst):
                if b is block:
                    return lst, i
                for body in b.bodies:
                    r = search(body)
                    if r:
                        return r
            return None
        for lst in [self.doc.main] + [f.bodies[0] for f in self.doc.functions] + [s.blocks for s in self.doc.loose]:
            r = search(lst)
            if r:
                return r
        return None

    def delete_selected(self) -> None:
        if isinstance(self.selected, bm.Note):
            self.push_undo()
            self.doc.notes = [n for n in self.doc.notes if n is not self.selected]
            self.selected = None
            self._mutated()
        elif isinstance(self.selected, bm.Block):
            self.delete_block(self.selected)

    def delete_block(self, block: bm.Block, with_below: bool = False) -> None:
        if block.kind == "def" and block in self.doc.functions:
            self.push_undo()
            self.doc.functions = [f for f in self.doc.functions if f is not block]
            self.selected = None
            self._mutated()
            return
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        if with_below:
            del lst[i:]
        else:
            del lst[i]
        self.doc.loose = [s for s in self.doc.loose if s.blocks]
        if self.selected is block:
            self.selected = None
        self._mutated()

    def duplicate_block(self, block: bm.Block) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        c = block.clone()
        c.blank_before = 0
        lst.insert(i + 1, c)
        self.selected = c
        self._mutated()

    def unwrap_block(self, block: bm.Block) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        lst[i:i + 1] = [b for body in block.bodies for b in body]
        self._mutated()

    def to_text_block(self, block: bm.Block) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        code = bm.blocks_to_code([block], customs=self.customs)
        if code.endswith("\n"):
            code = code[:-1]
        raw = bm.new_block("raw", code=code)
        raw.blank_before = block.blank_before
        lst[i] = raw
        self.selected = raw
        self._mutated()

    def raw_to_blocks(self, block: bm.Block) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        try:
            parsed = bm.code_to_blocks(block.fields.get("code", "") + "\n", self.macro_names, self.customs).main
        except bm.BlockParseError as exc:
            self.loose_label.setText(f"Can't turn that into blocks: {exc}")
            return
        if len(parsed) == 1 and parsed[0].kind == "raw":
            self.loose_label.setText("No blocks exist for that code -- it stays as custom code.")
            return
        self.push_undo()
        lst, i = loc
        if parsed:
            parsed[0].blank_before = block.blank_before
        lst[i:i + 1] = parsed
        self.selected = None
        self._mutated()

    def note_to_block(self, note: bm.Note) -> None:
        """Right-click a free note -> attach it at the end of the macro."""
        self.push_undo()
        self.doc.notes = [n for n in self.doc.notes if n is not note]
        cb = bm.new_block("comment")
        cb.fields["text"] = " " + note.text
        self.doc.main.append(cb)
        self._mutated()

    def add_elif(self, block: bm.Block) -> None:
        self.push_undo()
        n = block.branch_count()
        block.fields[f"cond{n}"] = bm.new_rep("compare")
        block.bodies.insert(n, [])
        block.header_trailing.insert(n, "")
        self._mutated()

    def remove_branch(self, block: bm.Block, k: int) -> None:
        self.push_undo()
        n = block.branch_count()
        if block.has_else and k == n:
            block.bodies.pop()
            block.has_else = False
            if len(block.header_trailing) > n:
                block.header_trailing.pop()
        elif 1 <= k < n:
            block.bodies.pop(k)
            if k < len(block.header_trailing):
                block.header_trailing.pop(k)
            for j in range(k, n - 1):
                block.fields[f"cond{j}"] = block.fields.get(f"cond{j + 1}", "True")
            block.fields.pop(f"cond{n - 1}", None)
        self._mutated()

    def add_else(self, block: bm.Block) -> None:
        if block.has_else:
            return
        self.push_undo()
        block.bodies.append([])
        block.has_else = True
        block.header_trailing.append("")
        self._mutated()

    def tidy(self) -> None:
        if not self.doc.loose and not self.doc.functions:
            return
        self.push_undo()
        x = self.doc.main_x + max(self.main_item.layout.width, 200) + 80
        y = self.doc.main_y
        for f in self.doc.functions:
            f.x, f.y = x, y + br.HAT_BUMP
            y += self.layouter().layout_stack(f.bodies[0], 0, 0, hat=f).height + 50
        for st in self.doc.loose:
            st.x, st.y = x, y
            y += self.layouter().layout_stack(st.blocks, 0, 0).height + 40
        self._mutated()

    # ------------------------------------------------------------------ custom blocks

    def create_custom_block(self) -> None:
        self._open_custom_dialog(None)

    def edit_custom_block(self, func: str) -> None:
        self._open_custom_dialog(self.customs.get(func))

    def _open_custom_dialog(self, defn) -> None:
        if self.custom_dialog_factory is None:
            return
        dlg = self.custom_dialog_factory(self, defn, self.customs, self.key_names)
        if dlg.exec() and dlg.result_def is not None:
            import custom_blocks
            if defn is not None and defn.func != dlg.result_def.func:
                self.customs.pop(defn.func, None)
            self.customs[dlg.result_def.func] = dlg.result_def
            custom_blocks.save(self.customs)
            self.palette_widget.refresh(force=True)
            self._rebuild()
            self.customs_changed.emit()

    def delete_custom_block(self, func: str) -> None:
        d = self.customs.get(func)
        if d is None:
            return
        if ask(self, f"Delete the custom block \"{d.name}\"?",
               "Macros that use it will show it as missing until you recreate it.", ["Cancel", "Delete"]) != 1:
            return
        import custom_blocks
        self.customs.pop(func, None)
        custom_blocks.save(self.customs)
        self.palette_widget.refresh(force=True)
        self._rebuild()
        self.customs_changed.emit()

    # ------------------------------------------------------------------ context menu

    def context_menu(self, hit: Hit, global_pos: QPoint) -> None:
        menu = QMenu(self)
        if hit.part == "note":
            note = hit.item.note
            menu.addAction("Edit note…", lambda: self._edit_note(hit.item))
            menu.addAction("Attach to the end of the macro", lambda: self.note_to_block(note))
            menu.addAction("Delete note", lambda: (self.select(note), self.delete_selected()))
            menu.exec(global_pos)
            return
        lb = hit.lb
        if lb.hat == "main":
            menu.addAction("Tidy up functions and loose blocks", self.tidy)
            menu.exec(global_pos)
            return
        b = lb.block
        if lb.hat == "def":
            menu.addAction("Delete this function (and its blocks)", lambda: self.delete_block(b))
            menu.exec(global_pos)
            return
        menu.addAction("Duplicate", lambda: self.duplicate_block(b))
        menu.addAction("Delete block", lambda: self.delete_block(b))
        menu.addAction("Delete this and everything below", lambda: self.delete_block(b, with_below=True))
        menu.addSeparator()
        if b.kind == "if":
            menu.addAction("Add \"else if\"", lambda: self.add_elif(b))
            if not b.has_else:
                menu.addAction("Add \"else\"", lambda: self.add_else(b))
            k = 0
            local = hit.item.mapFromScene(self.view.mapToScene(self.view.viewport().mapFromGlobal(global_pos)))
            for j, (ry, rh) in enumerate(lb.rows):
                if ry <= local.y() <= ry + rh:
                    k = j
            if k >= 1:
                label = "Remove this \"else\"" if (b.has_else and k == b.branch_count()) else "Remove this \"else if\""
                menu.addAction(label, lambda: self.remove_branch(b, k))
        if b.kind in bm.COMPOUND_KINDS:
            menu.addAction("Unwrap (keep the blocks inside)", lambda: self.unwrap_block(b))
        if b.kind == "call" and br.has_optional(b):
            menu.addAction("Hide unused options" if b.expanded else "Show all options",
                           lambda: self.toggle_expanded(b))
        if b.kind == "raw":
            if lb.fields:
                menu.addAction("Edit code…", lambda: self.click(Hit(hit.item, lb, lb.fields[0], "field")))
            menu.addAction("Turn into blocks", lambda: self.raw_to_blocks(b))
        elif b.kind != "comment":
            menu.addAction("Edit as custom code", lambda: self.to_text_block(b))
        menu.exec(global_pos)

    # ------------------------------------------------------------------ clicking / field editing

    def click(self, hit: Hit) -> None:
        if hit.part == "note":
            self._edit_note(hit.item)
            return
        if hit.part == "toggle":
            self.toggle_expanded(hit.lb.block)
            return
        if hit.fh is None:
            return
        lb, fh = hit.lb, hit.fh
        b = lb.block
        scene_rect = hit.item.mapRectToScene(fh.rect)
        view_rect = self.view.mapFromScene(scene_rect).boundingRect()
        gpos = self.view.viewport().mapToGlobal(view_rect.bottomLeft())
        key = fh.key
        rep = fh.rep
        if fh.kind == "cmpop" and rep is not None:
            self._menu(gpos, [(label, (lambda op=op: self._set_rep_name(b, fh.ref, op))) for op, label in bm.COMPARE_OPS])
            return
        if rep is not None:
            if rep.kind == "bool":
                self.set_socket(b, fh.ref, Rep("bool", "False" if rep.name == "True" else "True"))
            elif rep.kind == "mouse":
                self._menu(gpos, [(lbl, (lambda part=part: self.set_socket(b, fh.ref, Rep("mouse", part))))
                                  for part, lbl in (("", "mouse position (x, y)"), ("x", "mouse x"), ("y", "mouse y"))])
            elif rep.kind == "var":
                items = [(v, (lambda v=v: self.set_socket(b, fh.ref, Rep("var", v)))) for v in self.variables if v != rep.name]
                items.append(("Type a value instead…", lambda: self._open_line_editor(b, fh.ref, view_rect, "")))
                self._menu(gpos, items)
            return
        if fh.kind == "pick":
            if key == "@name":
                names = self.macro_names if b.kind == "macro_call" else self.function_names()
                items = [(n, (lambda n=n: self.set_socket(b, fh.ref, n))) for n in names]
                if not items:
                    items = [("(none yet)", None)]
                self._menu(gpos, items)
            elif key == "op":
                self._menu(gpos, [(op + "=", (lambda op=op: self.set_socket(b, fh.ref, op))) for op in bm.CHANGE_OPS])
            return
        if fh.kind == "choice":
            options = self._choice_options(b, key)
            items = [(o, (lambda o=o: self.set_socket(b, fh.ref, self._choice_value(o)))) for o in options]
            items.append(None)
            items.append(("Type a value…", lambda: self._open_line_editor(b, fh.ref, view_rect)))
            if self.variables:
                items.append(("Use variable", [(v, (lambda v=v: self.set_socket(b, fh.ref, Rep("var", v))))
                                               for v in self.variables]))
            p = br.param_default(b, key)
            if p is not None and p.default is not None:
                items.append(("Reset to default", lambda: self.set_socket(b, fh.ref, "")))
            self._menu(gpos, items)
            return
        if fh.kind == "code":
            self._open_text_editor(b, view_rect)
            return
        self._open_line_editor(b, fh.ref, view_rect)

    def _set_rep_name(self, b, ref, name) -> None:
        r = bm.get_socket(b, ref)
        if isinstance(r, Rep) and r.name != name:
            self.push_undo()
            r.name = name
            b.touch()
            self._mutated()

    def _choice_options(self, b, key) -> list:
        if b.kind == "call":
            p = br.param_default(b, key)
            if p is not None and p.kind == "bool":
                return ["true", "false"]
            if p is not None:
                return list(p.choices)
        if b.kind == "custom":
            import custom_blocks
            d = self.customs.get(b.name)
            a = next((a for a in d.args if a.name == key), None) if d else None
            if a is not None:
                return custom_blocks.arg_options(a, self.key_names) or []
        return []

    @staticmethod
    def _choice_value(o: str):
        if o == "true":
            return Rep("bool", "True")
        if o == "false":
            return Rep("bool", "False")
        return o

    def _menu(self, gpos, items) -> None:
        menu = QMenu(self)

        def fill(m, entries):
            for it in entries:
                if it is None:
                    m.addSeparator()
                    continue
                label, fn = it
                if isinstance(fn, list):
                    fill(m.addMenu(label), fn)
                elif fn is None:
                    m.addAction(label).setEnabled(False)
                else:
                    m.addAction(label, fn)
        fill(menu, items)
        menu.exec(gpos)

    def _editor_font(self, mono: bool = False) -> QFont:
        f = QFont(self.metrics.mono if mono else self.metrics.base)
        f.setPointSizeF(max(6.0, f.pointSizeF() * self.view.zoom))
        return f

    def _completions(self, b, key) -> list:
        words = list(self.variables)
        p = br.param_default(b, key)
        if (p is not None and p.kind in ("key", "keys")) or key in ("key", "left", "right", "button"):
            words += self.key_names
        if b.kind == "custom":
            import custom_blocks
            d = self.customs.get(b.name)
            a = next((a for a in d.args if a.name == key), None) if d else None
            if a is not None:
                words += custom_blocks.arg_completions(a, self.key_names)
        return words

    def _open_line_editor(self, b: bm.Block, ref: tuple, view_rect, initial=None) -> None:
        self.close_field_editor(commit=True)
        ed = QLineEdit(self.view.viewport())
        key = ref[0]
        if initial is None:
            value = bm.get_socket(b, ref)
            value = value if isinstance(value, str) else bm.expr_text(value)
            if b.kind == "comment":
                value = value[1:] if value.startswith(" ") else value
        else:
            value = initial
        ed.setText(value)
        ed.setFont(self._editor_font())
        t = self.theme
        ed.setStyleSheet(f"QLineEdit {{ background: {br.pill_fill(t).name()}; color: {br.pill_text(t).name()}; "
                         f"border: 2px solid {t.input_color().name()}; border-radius: 6px; padding: 0 6px; }}")
        p = br.param_default(b, ref[2][-1] if ref[2] else key)
        if p is not None and not ed.text() and p.default is not None:
            ed.setPlaceholderText(p.default)
        words = self._completions(b, ref[2][-1] if ref[2] else key)
        if words:
            comp = QCompleter(sorted(set(words)), ed)
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
            ed.setCompleter(comp)
        w = max(view_rect.width() + 60, 160)
        ed.setGeometry(view_rect.x() - 2, view_rect.y() - 2, int(w), view_rect.height() + 4)
        ed.returnPressed.connect(lambda: self.close_field_editor(commit=True))
        ed.editingFinished.connect(lambda: self.close_field_editor(commit=True))
        ed.show()
        ed.setFocus()
        ed.selectAll()
        self._field_editor = (ed, b, ref)
        ed.installEventFilter(self)

    def _open_text_editor(self, b: bm.Block, view_rect) -> None:
        self.close_field_editor(commit=True)
        ed = QPlainTextEdit(self.view.viewport())
        ed.setPlainText(b.fields.get("code", ""))
        ed.setFont(self._editor_font(mono=True))
        t = self.theme
        ed.setStyleSheet(f"QPlainTextEdit {{ background: {br.pill_fill(t).name()}; color: {br.pill_text(t).name()}; "
                         f"border: 2px solid {t.custom_color().name()}; border-radius: 6px; }}")
        ed.setToolTip("Ctrl+Enter or click away to apply, Esc to cancel")
        w = max(view_rect.width() + 80, 360)
        h = max(view_rect.height() + 40, 110)
        ed.setGeometry(view_rect.x() - 4, view_rect.y() - 4, int(w), int(h))
        ed.show()
        ed.setFocus()
        self._field_editor = (ed, b, ("code", None, ()))
        ed.installEventFilter(self)

    def _edit_note(self, item: NoteItem) -> None:
        self.close_field_editor(commit=True)
        w, h = item.size()
        rect = self.view.mapFromScene(item.mapRectToScene(QRectF(0, 0, w, h))).boundingRect()
        ed = QLineEdit(self.view.viewport())
        ed.setText(item.note.text)
        ed.setFont(self._editor_font())
        t = self.theme
        ed.setStyleSheet(f"QLineEdit {{ background: {br.pill_fill(t).name()}; color: {br.pill_text(t).name()}; "
                         f"border: 2px solid {t.input_color().name()}; border-radius: 6px; padding: 0 6px; }}")
        ed.setGeometry(rect.x(), rect.y(), max(rect.width() + 60, 200), rect.height())
        ed.returnPressed.connect(lambda: self.close_field_editor(commit=True))
        ed.editingFinished.connect(lambda: self.close_field_editor(commit=True))
        ed.show()
        ed.setFocus()
        ed.selectAll()
        self._field_editor = (ed, item.note, None)
        ed.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        fe = self._field_editor
        if fe is not None and obj is fe[0]:
            if ev.type() == ev.Type.KeyPress:
                if ev.key() == Qt.Key_Escape:
                    self.close_field_editor(commit=False)
                    return True
                if isinstance(obj, QPlainTextEdit) and ev.key() in (Qt.Key_Return, Qt.Key_Enter) \
                        and ev.modifiers() & Qt.ControlModifier:
                    self.close_field_editor(commit=True)
                    return True
            elif ev.type() == ev.Type.FocusOut and isinstance(obj, QPlainTextEdit):
                QTimer.singleShot(0, lambda: self.close_field_editor(commit=True))
        return super().eventFilter(obj, ev)

    def close_field_editor(self, commit: bool = True) -> None:
        fe = self._field_editor
        if fe is None:
            return
        self._field_editor = None
        ed, target, ref = fe
        ed.removeEventFilter(self)
        text = ed.toPlainText() if isinstance(ed, QPlainTextEdit) else ed.text()
        ed.hide()
        ed.deleteLater()
        if not commit:
            return
        if isinstance(target, bm.Note):
            text = text.strip()
            if text and text != target.text:
                self.push_undo()
                target.text = text
                self._mutated()
            return
        b = target
        key = ref[0]
        if b.kind == "comment":
            text = " " + text.strip() if text.strip() else ""
        elif b.kind == "raw":
            if not text.strip():
                text = "pass"
        else:
            text = text.strip()
        if key in ("@defname",) and text and not text.isidentifier():
            text = bm.re.sub(r"\W", "_", text)
        value = text
        # typing true/false/a known variable turns it into the matching reporter
        if b.kind not in ("raw", "comment") and key not in ("name", "var", "@defname", "params") \
                and not (b.kind in ("arguments", "def") and key == "params"):
            if text in ("True", "true"):
                value = Rep("bool", "True")
            elif text in ("False", "false"):
                value = Rep("bool", "False")
            elif text in self.variables:
                value = Rep("var", text)
        self.set_socket(b, ref, value)
