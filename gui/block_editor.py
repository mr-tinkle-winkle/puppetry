"""
The block editor: a palette of blocks on the left, a zoomable/pannable
canvas on the right (QGraphicsView). Drag blocks out of the palette,
snap them together, click a socket to type into it.

Interaction summary (also shown as the canvas tooltip):
  * drag a block      -> moves it AND everything below it (Ctrl: just it)
  * drop near a notch -> snaps (a dashed ghost shows where)
  * drop on palette   -> deletes
  * click a socket    -> edit it (dropdown for True/False and choices)
  * right-click       -> duplicate / delete / else-if / convert to text ...
  * drag empty space  -> pan;  Ctrl+wheel -> zoom;  Ctrl+0 -> reset zoom
  * Ctrl+Z / Ctrl+Shift+Z -> undo / redo;  Delete -> delete selected block

Only the stack under the "when ... pressed" hat is the macro. Anything
else left lying on the canvas is shown faded and is NOT saved -- the
editor says so in the toolbar.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QFont, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QCompleter, QGraphicsItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

import block_model as bm
import block_render as br
from reference import PRIMITIVES_BY_NAME
from ui_kit.custom_button import CustomButton
from ui_kit.custom_scrollbar import CustomScrollBar
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme, contrast_text
from widgets import dim_label, label_style, prompt_text, section_title

MIME_BLOCK = "application/x-puppetry-block"
MIME_VAR = "application/x-puppetry-variable"


# ---------------------------------------------------------------------------
# palette catalogue
# ---------------------------------------------------------------------------

def palette_sections(macro_names, variables) -> list:
    """[(title, category, [(spec, tooltip)])] -- spec is JSON-able and
    turns into a Block via spec_to_blocks()."""
    call = lambda n: ({"kind": "call", "name": n}, PRIMITIVES_BY_NAME[n].summary)  # noqa: E731
    sections = [
        ("Output — act on the world", "output",
         [call(n) for n in ("tap", "kd", "ku", "combo", "type", "move_mouse", "wheel", "command")]),
        ("Timing & control", "neutral",
         [call("wait"), call("speed"),
          ({"kind": "repeat"}, "Run the blocks inside N times."),
          ({"kind": "for"}, "Run the blocks inside once per item, with the item in a variable."),
          ({"kind": "while"}, "Keep running the blocks inside while the condition is true."),
          ({"kind": "if"}, "Run the blocks inside only if the condition is true."),
          ({"kind": "if", "has_else": True}, "One set of blocks if true, another if not."),
          call("checkpoint")]),
        ("Real input", "neutral", [call(n) for n in ("ignore", "ignore_keys", "actAs")]),
        ("Variables", "input",
         [({"kind": "arguments"}, "Declare this macro's parameters. Only goes at the very top."),
          ({"kind": "assign", "var": variables[0] if variables else "x"}, "Create or overwrite a variable."),
          ({"kind": "change", "var": variables[0] if variables else "x"}, "Add to (or -, *, /) a variable.")]),
    ]
    if macro_names:
        sections.append(("Other macros", "output",
                         [({"kind": "macro_call", "name": n}, f"Run the macro {n}.") for n in macro_names]))
    sections.append(("Other", "neutral",
                     [({"kind": "comment"}, "A note. Does nothing."),
                      ({"kind": "raw"}, "Any Python line(s) there's no block for.")]))
    return sections


def spec_to_blocks(spec: dict) -> list:
    kind = spec.get("kind")
    if kind == "call":
        return [bm.new_call(spec["name"])]
    kw = {k: v for k, v in spec.items() if k != "kind"}
    return [bm.new_block(kind, **kw)]


def render_blocks_pixmap(blocks, metrics, theme, scale: float = 1.0, variables=()) -> QPixmap:
    lay = br.Layouter(metrics, variables).layout_stack(blocks, 0, 0, allow_top_snap=False)
    w = max(10, int((lay.width + 4) * scale))
    h = max(10, int((lay.height + br.NOTCH_D + 4) * scale))
    ratio = 2.0
    pm = QPixmap(int(w * ratio), int(h * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.scale(scale, scale)
    p.translate(1, 1)
    for lb in lay.blocks:
        br.paint_block(p, lb, metrics, theme)
    p.end()
    return pm


class PaletteBlock(QLabel):
    """One draggable template in the palette."""

    def __init__(self, spec: dict, tooltip: str, metrics, theme, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._press: QPoint | None = None
        blocks = spec_to_blocks(spec)
        self.setPixmap(render_blocks_pixmap(blocks, metrics, theme, 0.8))
        self.setToolTip(tooltip)
        self.setCursor(Qt.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e) -> None:
        if self._press is None or (e.position().toPoint() - self._press).manhattanLength() < 5:
            return
        drag = QDrag(self)
        mime = QMimeData()
        hot = self._press
        mime.setData(MIME_BLOCK, json.dumps({"spec": self.spec, "hx": hot.x() / 0.8, "hy": hot.y() / 0.8}).encode())
        drag.setMimeData(mime)
        drag.setPixmap(self.pixmap())
        drag.setHotSpot(hot)
        self._press = None
        drag.exec(Qt.CopyAction)

    def mouseReleaseEvent(self, e) -> None:
        self._press = None


class VariableChip(QLabel):
    """A variable "reporter": drag it onto any socket to use the variable."""

    def __init__(self, name: str, theme: Theme, parent=None):
        super().__init__(name, parent)
        self.name = name
        c = theme.input_color()
        self.setStyleSheet(f"QLabel {{ background: {c.name()}; color: {contrast_text(c).name()}; "
                           f"border-radius: 11px; padding: 3px 12px; }}")
        self.setToolTip(f"Drag onto any socket to use {name}.")
        self.setCursor(Qt.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._press = None

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e) -> None:
        if self._press is None or (e.position().toPoint() - self._press).manhattanLength() < 5:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_VAR, self.name.encode())
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(self._press)
        self._press = None
        drag.exec(Qt.CopyAction)


class Palette(QWidget):
    def __init__(self, editor: "BlockEditor", parent=None):
        super().__init__(parent)
        self.editor = editor
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(4, 4, 8, 4)
        self._lay.setSpacing(6)
        self._key = None

    def refresh(self, macro_names, variables, extra_vars) -> None:
        allv = list(dict.fromkeys(list(variables) + list(extra_vars)))
        key = (tuple(macro_names), tuple(allv))
        if key == self._key:
            return
        self._key = key
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        theme = self.editor.theme
        m = self.editor.metrics
        for title, cat, entries in palette_sections(macro_names, allv):
            head = section_title(title)
            head.setStyleSheet(label_style(theme.category_color(cat), "font-size: 14px; font-weight: bold;"))
            self._lay.addWidget(head)
            for spec, tip in entries:
                self._lay.addWidget(PaletteBlock(spec, tip, m, theme))
            if title == "Variables":
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(6)
                for name in allv[:12]:
                    rl.addWidget(VariableChip(name, theme))
                rl.addStretch(1)
                if allv:
                    self._lay.addWidget(row)
                newv = CustomButton("+ New variable")
                newv.clicked.connect(self.editor.new_variable)
                r2 = QHBoxLayout()
                r2.addWidget(newv)
                r2.addStretch(1)
                w = QWidget()
                w.setLayout(r2)
                r2.setContentsMargins(0, 0, 0, 0)
                self._lay.addWidget(w)
        self._lay.addWidget(dim_label("Drag blocks onto the canvas. Drop a block back here to delete it."))
        self._lay.addStretch(1)


# ---------------------------------------------------------------------------
# canvas items
# ---------------------------------------------------------------------------

class StackItem(QGraphicsItem):
    """One stack of blocks (the main script, or a loose/dragged one)."""

    def __init__(self, editor: "BlockEditor", blocks: list, stack: "bm.Stack | None", is_main: bool):
        super().__init__()
        self.editor = editor
        self.blocks = blocks
        self.stack = stack
        self.is_main = is_main
        self.layout: br.StackLayout | None = None
        self.setFlag(QGraphicsItem.ItemUsesExtendedStyleOption, True)
        self.relayout()

    def relayout(self) -> None:
        self.prepareGeometryChange()
        lay = br.Layouter(self.editor.metrics, self.editor.variables)
        self.layout = lay.layout_stack(self.blocks, 0, 0, hat_text=self.editor.hat_text if self.is_main else None,
                                       allow_top_snap=not self.is_main)
        if not self.is_main and self.stack is not None:
            self.setOpacity(1.0 if self.editor.dragging_item is self else 0.6)

    def boundingRect(self) -> QRectF:
        l = self.layout
        return QRectF(-4, -br.HAT_BUMP - 4, (l.width if l else 0) + 60, (l.height if l else 0) + br.HAT_BUMP + 20)

    def paint(self, p: QPainter, option, widget=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        ed = self.editor
        exposed = option.exposedRect
        for lb in self.layout.visible(exposed):
            br.paint_block(p, lb, ed.metrics, ed.theme, selected=(lb.block is ed.selected),
                           drop_field=(ed.drop_field[1] if ed.drop_field and ed.drop_field[0] is lb.block else None))

    def hit(self, pos: QPointF):
        """(LaidBlock, part, key) at local pos, topmost first, or None.
        part: 'field' | 'toggle' | 'block'."""
        r = QRectF(pos.x(), pos.y(), 1, 1)
        for lb in reversed(self.layout.visible(r)):
            if not lb.rect.contains(pos):
                continue
            if lb.toggle is not None and lb.toggle.contains(pos):
                return lb, "toggle", None
            for fh in lb.fields:
                if fh.rect.contains(pos):
                    return lb, "field", fh
            if lb.path().contains(pos):
                return lb, "block", None
        return None


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
        self.zoom = 1.0
        self._press = None      # (scene_pos, view_pos, hit, stack_item)
        self._pan = None
        self._tip = ""

    # -- zoom --------------------------------------------------------------
    def set_zoom(self, z: float) -> None:
        z = max(0.35, min(2.5, z))
        f = z / self.zoom
        self.zoom = z
        self.scale(f, f)
        self.editor.close_field_editor(commit=True)
        self.editor.zoom_changed()

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
                ed.select(hit[1].block if not hit[1].hat else None)
                ed.context_menu(hit, e.globalPosition().toPoint())
            return
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and hit is None):
            ed.select(None)
            self._pan = (e.position().toPoint(), self.horizontalScrollBar().value(), self.verticalScrollBar().value())
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            self._press = (sp, e.position().toPoint(), hit, e.modifiers())
            ed.select(hit[1].block if not hit[1].hat else None)

    def mouseMoveEvent(self, e) -> None:
        ed = self.editor
        if self._pan is not None:
            start, hx, vy = self._pan
            d = e.position().toPoint() - start
            self.horizontalScrollBar().setValue(hx - d.x())
            self.verticalScrollBar().setValue(vy - d.y())
            return
        sp = self.mapToScene(e.position().toPoint())
        if ed.dragging_item is not None:
            ed.drag_move(sp)
            self._autoscroll(e.position().toPoint())
            return
        if self._press is not None:
            if (e.position().toPoint() - self._press[1]).manhattanLength() > 6:
                psp, _vp, hit, mods = self._press
                self._press = None
                ed.begin_drag(hit, psp, single=bool(mods & Qt.ControlModifier))
                ed.drag_move(sp)
            return
        hit = ed.hit_test(sp)
        cur = Qt.ArrowCursor
        if hit:
            cur = Qt.IBeamCursor if hit[2] and hit[2].shape in ("pill", "hex", "box", "text") else Qt.OpenHandCursor
            if hit[3] == "toggle":
                cur = Qt.PointingHandCursor
        self.viewport().setCursor(cur)
        tip = ""
        if hit and not hit[1].hat:
            b = hit[1].block
            if hit[3] == "toggle":
                tip = "Show / hide this block's optional settings"
            elif b.kind == "call" and b.name in PRIMITIVES_BY_NAME:
                tip = PRIMITIVES_BY_NAME[b.name].summary
            elif b.kind == "raw":
                tip = "Python with no block of its own -- click to edit, right-click to try turning it into blocks"
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
        if ed.dragging_item is not None:
            ed.end_drag(self.mapToScene(e.position().toPoint()))
            return
        if self._press is not None:
            _sp, _vp, hit, _m = self._press
            self._press = None
            if hit:
                stack, part, fh = hit[0], hit[1], hit[2]
                lb = part
                if hit[3] == "toggle":
                    ed.toggle_expanded(lb.block)
                elif fh is not None:
                    ed.edit_field(lb, fh, stack)

    def mouseDoubleClickEvent(self, e) -> None:
        self.mousePressEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.editor.selected is not None:
            self.editor.delete_block(self.editor.selected)
            return
        if e.modifiers() & Qt.ControlModifier and e.key() == Qt.Key_0:
            self.set_zoom(1.0)
            return
        super().keyPressEvent(e)

    # -- drops from the palette ---------------------------------------------
    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasFormat(MIME_BLOCK) or md.hasFormat(MIME_VAR):
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
        elif md.hasFormat(MIME_VAR):
            ok = self.editor.variable_drag_move(sp)
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
        elif md.hasFormat(MIME_VAR):
            self.editor.variable_drop(bytes(md.data(MIME_VAR)).decode(), sp)
            e.acceptProposedAction()

    def drawBackground(self, p: QPainter, rect: QRectF) -> None:
        bg = self.editor.theme.page_background()
        p.fillRect(rect, bg)
        dot = QColor(self.editor.theme.text())
        dot.setAlpha(28)
        step = 24
        if self.zoom < 0.5:
            return
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
    python_needed = Signal()    # a Python-only block was just added

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = Theme()
        self.metrics = br.Metrics()
        self.doc = bm.Doc()
        self.hat_text = "when this macro runs"
        self.macro_names: list[str] = []
        self.key_names: list[str] = []
        self.extra_vars: list[str] = []
        self.variables: list[str] = []
        self.selected: bm.Block | None = None
        self.dragging_item: StackItem | None = None
        self._drag_offset = QPointF()
        self._drag_from_palette = None   # (spec, blocks) while a palette drag hovers
        self.snap: br.SnapTarget | None = None
        self._ghost = None
        self.drop_field = None           # (block, key)
        self._undo: list = []
        self._redo: list = []
        self.revision = 0
        self._field_editor = None
        self._items: list[StackItem] = []

        self.scene = QGraphicsScene(self)
        self.ghost_item = GhostItem(self)
        self.scene.addItem(self.ghost_item)
        pal = self.palette()
        pal.setColor(QPalette.Base, self.theme.page_background())

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
        self.tidy_btn.setToolTip("Line up loose stacks to the right of the macro")
        self.tidy_btn.clicked.connect(self.tidy)
        self.zoom_label = CustomButton("100%")
        self.zoom_label.setToolTip("Reset zoom (Ctrl+0). Ctrl+scroll to zoom.")
        self.zoom_label.clicked.connect(lambda: self.view.set_zoom(1.0))
        for b in (self.undo_btn, self.redo_btn, self.tidy_btn, self.zoom_label):
            tools.addWidget(b)
        right.addLayout(tools)
        self.view = BlockView(self)
        self.view.setToolTip("")
        right.addWidget(self.view, stretch=1)
        root.addLayout(right, stretch=1)

        for seq, fn in ((QKeySequence("Ctrl+Z"), self.undo), (QKeySequence("Ctrl+Shift+Z"), self.redo),
                        (QKeySequence("Ctrl+Y"), self.redo)):
            sc = QShortcut(seq, self.view)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

        self._rebuild()

    # ------------------------------------------------------------------ public API

    def set_doc(self, doc: bm.Doc, reset_view: bool = True) -> None:
        self.close_field_editor(commit=False)
        self.doc = doc
        self.selected = None
        self._undo.clear()
        self._redo.clear()
        self._rebuild()
        if reset_view:
            self.view.horizontalScrollBar().setValue(self.view.horizontalScrollBar().minimum())
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().minimum())

    def code(self) -> str:
        self.close_field_editor(commit=True)
        return bm.blocks_to_code(self.doc)

    def set_context(self, macro_names=None, hat_text=None, key_names=None) -> None:
        if macro_names is not None:
            self.macro_names = list(macro_names)
        if key_names is not None:
            self.key_names = list(key_names)
        if hat_text is not None and hat_text != self.hat_text:
            self.hat_text = hat_text
            self._rebuild()
        self._refresh_palette()

    def loose_count(self) -> int:
        return sum(len(list(bm.walk(s.blocks))) for s in self.doc.loose)

    def block_count(self) -> int:
        return len(list(bm.walk(self.doc.main)))

    # ------------------------------------------------------------------ build

    def _rebuild(self) -> None:
        self.variables = bm.variable_names(self.doc.main) + [v for s in self.doc.loose
                                                             for v in bm.variable_names(s.blocks)]
        keep_drag = self.dragging_item
        for it in self._items:
            if it is not keep_drag and it.scene() is self.scene:
                self.scene.removeItem(it)
        self._items = []
        main = StackItem(self, self.doc.main, None, True)
        main.setPos(self.doc.main_x, self.doc.main_y)
        self.scene.addItem(main)
        self._items.append(main)
        self.main_item = main
        for st in self.doc.loose:
            it = StackItem(self, st.blocks, st, False)
            it.setPos(st.x, st.y)
            self.scene.addItem(it)
            self._items.append(it)
        if keep_drag is not None:
            self._items.append(keep_drag)
        self._update_scene_rect()
        n = self.loose_count()
        self.loose_label.setText(
            f"{n} loose block{'s' if n != 1 else ''} not attached under the hat -- "
            "they're not part of the macro and won't be saved." if n else "")
        self.undo_btn.setEnabled(bool(self._undo))
        self.redo_btn.setEnabled(bool(self._redo))
        self._refresh_palette()
        self.scene.update()

    def _refresh_palette(self) -> None:
        self.palette_widget.refresh(self.macro_names, self.variables, self.extra_vars)

    def _update_scene_rect(self) -> None:
        r = QRectF()
        for it in self._items:
            r = r.united(it.sceneBoundingRect())
        r = r.adjusted(-60, -60, 1200, 800)
        r = r.united(QRectF(self.doc.main_x - 40, self.doc.main_y - 50, 1600, 1000))
        self.scene.setSceneRect(r)

    def zoom_changed(self) -> None:
        self.zoom_label.setText(f"{round(self.view.zoom * 100)}%")

    # ------------------------------------------------------------------ undo

    def _snapshot(self):
        d = self.doc
        return (bm.copy_blocks(d.main), [bm.Stack(bm.copy_blocks(st.blocks), st.x, st.y) for st in d.loose],
                d.main_x, d.main_y)

    def push_undo(self) -> None:
        self._undo.append(self._snapshot())
        if len(self._undo) > 60:
            self._undo.pop(0)
        self._redo.clear()

    def _restore(self, snap) -> None:
        main, loose, mx, my = snap
        self.doc.main[:] = main
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

    def _mutated(self, check_python: bool = False) -> None:
        self.revision += 1
        self._rebuild()
        if check_python and bm.needs_python(self.doc.main):
            self.python_needed.emit()
        self.changed.emit()

    # ------------------------------------------------------------------ hit testing / selection

    def hit_test(self, sp: QPointF):
        """(stack_item, LaidBlock, FieldHit|None, part) or None."""
        for it in reversed(self._items):
            if it is self.dragging_item:
                continue
            local = it.mapFromScene(sp)
            h = it.hit(local)
            if h:
                lb, part, fh = h
                return it, lb, fh, part
        return None

    def select(self, block) -> None:
        if block is not self.selected:
            self.selected = block
            self.scene.update()

    # ------------------------------------------------------------------ dragging blocks

    def begin_drag(self, hit, press_sp: QPointF, single: bool = False) -> None:
        it, lb, _fh, _part = hit
        if lb.hat:
            self.push_undo()
            self.dragging_item = it
            self._drag_main = True
            self._drag_offset = press_sp - it.pos()
            return
        self._drag_main = False
        self.push_undo()
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
        item = StackItem(self, st.blocks, st, False)
        item.setPos(origin)
        item.setZValue(1000)
        self.scene.addItem(item)
        self.dragging_item = item
        item.relayout()
        self._drag_offset = press_sp - origin
        self._rebuild()

    def drag_move(self, sp: QPointF) -> None:
        it = self.dragging_item
        if it is None:
            return
        it.setPos(sp - self._drag_offset)
        if getattr(self, "_drag_main", False):
            self.doc.main_x, self.doc.main_y = it.pos().x(), it.pos().y()
            return
        top_left = it.pos()
        blocks = it.blocks
        self._find_snap(top_left, blocks, it.layout.height)
        self.view.viewport().update()

    @property
    def ghost(self):
        return self._ghost

    @ghost.setter
    def ghost(self, value) -> None:
        self._ghost = value
        if hasattr(self, "ghost_item"):
            self.ghost_item.sync()

    def _find_snap(self, top_left: QPointF, blocks: list, height: float) -> None:
        best, best_d = None, br.SNAP_DIST
        first = blocks[0] if blocks else None
        dragged_args = first is not None and first.kind == "arguments"
        main_has_args = bool(self.doc.main) and self.doc.main[0].kind == "arguments"
        bottom_left = QPointF(top_left.x(), top_left.y() + height)
        for item in self._items:
            if item is self.dragging_item:
                continue
            ox, oy = item.pos().x(), item.pos().y()
            for s in item.layout.snaps:
                if s.kind == "top":
                    if dragged_args:
                        continue
                    ref = bottom_left
                else:
                    ref = top_left
                d = ((s.x + ox - ref.x()) ** 2 + (s.y + oy - ref.y()) ** 2) ** 0.5
                if d >= best_d:
                    continue
                # arguments() only ever goes first in the macro
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
        lay = br.Layouter(self.metrics, self.variables).layout_stack(blocks[:1], 0, 0, allow_top_snap=False)
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
        it = self.dragging_item
        self.dragging_item = None
        if getattr(self, "_drag_main", False):
            self._drag_main = False
            self.doc.main_x, self.doc.main_y = it.pos().x(), it.pos().y()
            self._mutated()
            return
        self.scene.removeItem(it)
        blocks = it.blocks
        snap, self.snap, self.ghost = self.snap, None, None
        if self._over_palette():
            if self.selected is not None and any(self.selected is b for b in bm.walk(blocks)):
                self.selected = None
        elif snap is not None:
            self._insert(snap, blocks, it.pos())
        else:
            self.doc.loose.append(bm.Stack(blocks, it.pos().x(), it.pos().y()))
        self._mutated(check_python=True)
        self.view.viewport().update()

    def _insert(self, snap: br.SnapTarget, blocks: list, pos: QPointF) -> None:
        if snap.kind == "top":
            snap.container[0:0] = blocks
            owner = next((s for s in self.doc.loose if s.blocks is snap.container), None)
            if owner is not None:
                owner.x, owner.y = pos.x(), pos.y()
        else:
            snap.container[snap.index:snap.index] = blocks

    # -- palette drags -----------------------------------------------------
    def external_drag_move(self, info: dict, sp: QPointF) -> None:
        spec = info["spec"]
        if self._drag_from_palette is None or self._drag_from_palette[0] != spec:
            self._drag_from_palette = (spec, spec_to_blocks(spec))
        blocks = self._drag_from_palette[1]
        top_left = QPointF(sp.x() - info.get("hx", 10), sp.y() - info.get("hy", 10))
        lay = br.Layouter(self.metrics, self.variables).layout_stack(blocks, 0, 0, allow_top_snap=False)
        self._find_snap(top_left, blocks, lay.height)
        self.view.viewport().update()

    def external_drop(self, info: dict, sp: QPointF) -> None:
        spec = info["spec"]
        blocks = spec_to_blocks(spec)
        top_left = QPointF(sp.x() - info.get("hx", 10), sp.y() - info.get("hy", 10))
        lay = br.Layouter(self.metrics, self.variables).layout_stack(blocks, 0, 0, allow_top_snap=False)
        self._find_snap(top_left, blocks, lay.height)
        snap = self.snap
        self.clear_hover_state()
        self.push_undo()
        if snap is not None:
            self._insert(snap, blocks, top_left)
        else:
            self.doc.loose.append(bm.Stack(blocks, top_left.x(), top_left.y()))
        self.selected = blocks[0]
        self._mutated(check_python=True)

    def add_blocks(self, blocks: list, snap: "br.SnapTarget | None" = None) -> None:
        """Programmatic insert (tests, and 'append to macro'): at `snap`, or
        at the end of the main script."""
        self.push_undo()
        if snap is None:
            self.doc.main.extend(blocks)
        else:
            self._insert(snap, blocks, QPointF())
        self._mutated(check_python=True)

    def clear_hover_state(self) -> None:
        self.snap, self.ghost, self.drop_field = None, None, None
        self._drag_from_palette = None
        self.view.viewport().update()
        self.scene.update()

    # -- variable drags ------------------------------------------------------
    def variable_drag_move(self, sp: QPointF) -> bool:
        hit = self.hit_test(sp)
        target = None
        if hit and hit[2] is not None and hit[2].shape in ("pill", "hex") and hit[2].key not in ("op", "@macro"):
            target = (hit[1].block, hit[2].key)
        if target != self.drop_field:
            self.drop_field = target
            self.scene.update()
        return target is not None

    def variable_drop(self, name: str, sp: QPointF) -> None:
        if not self.variable_drag_move(sp):
            self.clear_hover_state()
            return
        block, key = self.drop_field
        self.clear_hover_state()
        self.set_field(block, key, name)

    def new_variable(self) -> None:
        name = prompt_text(self, "New variable", "")
        if name is None:
            return
        name = name.strip().replace(" ", "_")
        if not name.isidentifier():
            return
        if name not in self.extra_vars:
            self.extra_vars.append(name)
        self._refresh_palette()

    # ------------------------------------------------------------------ edits

    def set_field(self, block: bm.Block, key: str, value: str) -> None:
        old = block.name if key == "@macro" else block.fields.get(key, "")
        if value == old:
            return
        self.push_undo()
        if key == "@macro":
            block.name = value
        else:
            block.fields[key] = value
            block.kw.discard(key) if key in block.kw and not value.strip() else None
        block.touch()
        self._mutated(check_python=True)

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
        r = search(self.doc.main)
        if r:
            return r
        for st in self.doc.loose:
            r = search(st.blocks)
            if r:
                return r
        return None

    def delete_block(self, block: bm.Block, with_below: bool = False) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        if with_below:
            del lst[i:]
        else:
            del lst[i]
        self._drop_empty_loose()
        if self.selected is block:
            self.selected = None
        self._mutated()

    def _drop_empty_loose(self) -> None:
        self.doc.loose = [s for s in self.doc.loose if s.blocks]

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
        self._mutated(check_python=True)

    def unwrap_block(self, block: bm.Block) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        children = [b for body in block.bodies for b in body]
        lst[i:i + 1] = children
        self._mutated()

    def to_text_block(self, block: bm.Block) -> None:
        loc = self._find_container(block)
        if not loc:
            return
        self.push_undo()
        lst, i = loc
        code = bm.blocks_to_code([block])
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
            parsed = bm.code_to_blocks(block.fields.get("code", "") + "\n", self.macro_names).main
        except bm.BlockParseError as exc:
            self.loose_label.setText(f"Can't turn that into blocks: {exc}")
            return
        if len(parsed) == 1 and parsed[0].kind == "raw":
            self.loose_label.setText("No blocks exist for that code -- it stays as text.")
            return
        self.push_undo()
        lst, i = loc
        if parsed:
            parsed[0].blank_before = block.blank_before
        lst[i:i + 1] = parsed
        self.selected = None
        self._mutated(check_python=True)

    def add_elif(self, block: bm.Block) -> None:
        self.push_undo()
        n = block.branch_count()
        # shift nothing: conditions are cond0..cond{n-1}; new one is cond{n}
        block.fields[f"cond{n}"] = "True"
        block.bodies.insert(n, [])
        block.header_trailing.insert(n, "")
        self._mutated()

    def remove_branch(self, block: bm.Block, k: int) -> None:
        """Remove else-if branch k (k >= 1) or the else (k == branch_count())."""
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
        if not self.doc.loose:
            return
        self.push_undo()
        x = self.doc.main_x + self.main_item.layout.width + 80
        y = self.doc.main_y
        for st in self.doc.loose:
            st.x, st.y = x, y
            lay = br.Layouter(self.metrics, self.variables).layout_stack(st.blocks, 0, 0)
            y += lay.height + 40
        self._mutated()

    # ------------------------------------------------------------------ context menu

    def context_menu(self, hit, global_pos: QPoint) -> None:
        it, lb, fh, part = hit
        menu = QMenu(self)
        if lb.hat:
            menu.addAction("Tidy up loose blocks", self.tidy)
            menu.exec(global_pos)
            return
        b = lb.block
        menu.addAction("Duplicate", lambda: self.duplicate_block(b))
        menu.addAction("Delete block", lambda: self.delete_block(b))
        menu.addAction("Delete this and everything below", lambda: self.delete_block(b, with_below=True))
        menu.addSeparator()
        if b.kind == "if":
            menu.addAction("Add \"else if\"", lambda: self.add_elif(b))
            if not b.has_else:
                menu.addAction("Add \"else\"", lambda: self.add_else(b))
            # which header row was clicked?
            k = 0
            if it is not None:
                local = it.mapFromScene(self.view.mapToScene(self.view.viewport().mapFromGlobal(global_pos)))
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
            menu.addAction("Edit code…", lambda: self.edit_field(lb, lb.fields[0], it) if lb.fields else None)
            menu.addAction("Turn into blocks", lambda: self.raw_to_blocks(b))
        elif b.kind != "comment":
            menu.addAction("Edit as text", lambda: self.to_text_block(b))
        menu.exec(global_pos)

    # ------------------------------------------------------------------ field editing

    def edit_field(self, lb: br.LaidBlock, fh: br.FieldHit, item: StackItem) -> None:
        b = lb.block
        key = fh.key
        scene_rect = item.mapRectToScene(fh.rect)
        view_rect = self.view.mapFromScene(scene_rect).boundingRect()
        gpos = self.view.viewport().mapToGlobal(view_rect.bottomLeft())
        # dropdown-style sockets
        options = None
        if key == "@macro":
            options = list(self.macro_names)
        elif b.kind == "change" and key == "op":
            options = [op + "=" for op in bm.CHANGE_OPS]
        elif b.kind == "call":
            p = bm.prim_param(b.name, key)
            if p is not None and p.kind == "bool":
                options = list(("True", "False"))
            elif p is not None and p.kind == "choice":
                options = list(p.choices)
        if options is not None:
            menu = QMenu(self)
            if not options:
                menu.addAction("(no other macros yet)").setEnabled(False)
            for opt in options:
                menu.addAction(opt, lambda o=opt: self._apply_option(b, key, o))
            if key not in ("@macro", "op"):
                menu.addSeparator()
                menu.addAction("Type a value…", lambda: self._open_line_editor(b, key, view_rect))
                if self.variables:
                    sub = menu.addMenu("Use variable")
                    for v in self.variables:
                        sub.addAction(v, lambda v=v: self.set_field(b, key, v))
                p = bm.prim_param(b.name, key) if b.kind == "call" else None
                if p is not None and p.default is not None:
                    menu.addAction("Reset to default", lambda: self.set_field(b, key, ""))
            menu.exec(gpos)
            return
        if b.kind == "raw":
            self._open_text_editor(b, view_rect)
            return
        self._open_line_editor(b, key, view_rect)

    def _apply_option(self, b, key, opt) -> None:
        if key == "op":
            self.set_field(b, "op", opt[:-1])
        else:
            self.set_field(b, key, opt)

    def _editor_font(self, mono: bool = False) -> QFont:
        f = QFont(self.metrics.mono if mono else self.metrics.base)
        f.setPointSizeF(max(6.0, f.pointSizeF() * self.view.zoom))
        return f

    def _open_line_editor(self, b: bm.Block, key: str, view_rect) -> None:
        self.close_field_editor(commit=True)
        ed = QLineEdit(self.view.viewport())
        value = b.fields.get(key, "")
        if b.kind == "comment":
            value = value[1:] if value.startswith(" ") else value
        ed.setText(value)
        ed.setFont(self._editor_font())
        t = self.theme
        ed.setStyleSheet(f"QLineEdit {{ background: {br.pill_fill(self.theme).name()}; color: {br.pill_text(self.theme).name()}; "
                         f"border: 2px solid {t.input_color().name()}; border-radius: 6px; padding: 0 6px; }}")
        p = bm.prim_param(b.name, key) if b.kind == "call" else None
        if p is not None and not ed.text() and p.default is not None:
            ed.setPlaceholderText(p.default)
        words = list(self.variables)
        if p is not None and p.kind in ("key", "keys"):
            words += self.key_names
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
        self._field_editor = (ed, b, key)
        ed.installEventFilter(self)

    def _open_text_editor(self, b: bm.Block, view_rect) -> None:
        self.close_field_editor(commit=True)
        ed = QPlainTextEdit(self.view.viewport())
        ed.setPlainText(b.fields.get("code", ""))
        ed.setFont(self._editor_font(mono=True))
        ed.setStyleSheet(f"QPlainTextEdit {{ background: {br.pill_fill(self.theme).name()}; color: {br.pill_text(self.theme).name()}; "
                         f"border: 2px solid {self.theme.input_color().name()}; border-radius: 6px; }}")
        ed.setToolTip("Ctrl+Enter or click away to apply, Esc to cancel")
        w = max(view_rect.width() + 80, 360)
        h = max(view_rect.height() + 40, 110)
        ed.setGeometry(view_rect.x() - 4, view_rect.y() - 4, int(w), int(h))
        ed.show()
        ed.setFocus()
        self._field_editor = (ed, b, "code")
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
        ed, b, key = fe
        ed.removeEventFilter(self)
        text = ed.toPlainText() if isinstance(ed, QPlainTextEdit) else ed.text()
        ed.hide()
        ed.deleteLater()
        if not commit:
            return
        if b.kind == "comment":
            text = " " + text.strip() if text.strip() else ""
        elif b.kind != "raw":
            text = text.strip()
        if b.kind == "raw" and not text.strip():
            text = "pass"
        self.set_field(b, key, text)
