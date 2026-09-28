"""
Block geometry + painting for the block editor: puzzle-piece silhouettes
(a notch cut into every top edge, a matching tab under every bottom edge,
C-shaped mouths for loops/ifs), socket "pills" for each argument, and the
layout pass that turns a Block tree into positioned shapes.

Layout is deliberately cheap: it only computes numbers. QPainterPaths are
built lazily the first time a block is actually painted or hit-tested, so
a 20k-line transcribed macro lays out without building 20k paths.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from block_model import COMPOUND_KINDS, Block
from reference import PRIMITIVES_BY_NAME
from ui_kit.theme import Theme, contrast_text

# ---- geometry (scene units) -------------------------------------------------
PAD_X = 10.0
MIN_H = 34.0
MIN_W = 70.0
NOTCH_X = 14.0      # notch starts this far from the block's left edge
NOTCH_W = 26.0      # notch width at the top
NOTCH_S = 6.0       # slope inset
NOTCH_D = 6.0       # depth
RADIUS = 5.0
ARM_W = 18.0        # C-block left arm
MOUTH_MIN = 26.0    # empty mouth height
ARM_H = 22.0        # C-block bottom arm
HAT_BUMP = 16.0
FIELD_H = 24.0
FIELD_PAD = 8.0
GAP = 6.0
SNAP_DIST = 30.0

CONDITION_KEYS = ("cond",)


def fonts():
    base = QFont()
    base.setPointSizeF(10.0)
    bold = QFont(base)
    bold.setBold(True)
    mono = QFont("monospace")
    mono.setStyleHint(QFont.Monospace)
    mono.setPointSizeF(9.5)
    italic = QFont(base)
    italic.setItalic(True)
    return base, bold, mono, italic


class Metrics:
    """Cached text widths (layout measures the same strings constantly)."""

    def __init__(self):
        self.base, self.bold, self.mono, self.italic = fonts()
        self._fm = {id(f): QFontMetricsF(f) for f in (self.base, self.bold, self.mono, self.italic)}
        self._cache: dict = {}

    def width(self, font: QFont, text: str) -> float:
        key = (id(font), text)
        w = self._cache.get(key)
        if w is None:
            w = self._fm[id(font)].horizontalAdvance(text)
            if len(self._cache) > 50000:
                self._cache.clear()
            self._cache[key] = w
        return w

    def line_h(self, font: QFont) -> float:
        return self._fm[id(font)].height()


# ---- block content ---------------------------------------------------------

def block_category(b: Block) -> str:
    if b.kind == "call":
        prim = PRIMITIVES_BY_NAME.get(b.name)
        return prim.category if prim else "neutral"
    if b.kind in ("arguments",):
        return "input"
    if b.kind in ("assign", "change", "macro_call"):
        return "output"
    return "neutral"


def block_color(b: Block, theme: Theme) -> QColor:
    if b.kind == "comment":
        return theme.neutral_block_color().darker(150)
    if b.kind == "raw":
        return theme.neutral_block_color().darker(125)
    if b.kind in COMPOUND_KINDS:
        return theme.neutral_block_color().darker(108)
    return theme.category_color(block_category(b))


def optional_hidden(b: Block) -> bool:
    """True if a call block has optional sockets that are currently hidden."""
    if b.kind != "call" or b.expanded:
        return False
    prim = PRIMITIVES_BY_NAME.get(b.name)
    return bool(prim) and any(not p.required and not p.vararg and not (b.fields.get(p.name) or "").strip()
                              for p in prim.params)


def has_optional(b: Block) -> bool:
    prim = PRIMITIVES_BY_NAME.get(b.name) if b.kind == "call" else None
    return bool(prim) and any(not p.required and not p.vararg for p in prim.params)


def header_parts(b: Block, branch: int = 0) -> list:
    """[(kind, payload)] -- kind: name | label | field | toggle | trail."""
    f = b.fields
    k = b.kind
    if k == "call":
        prim = PRIMITIVES_BY_NAME.get(b.name)
        parts = [("name", b.name)]
        if prim:
            for p in prim.params:
                shown = p.required or p.vararg or b.expanded or (f.get(p.name) or "").strip()
                if not shown:
                    continue
                if p.caption:
                    parts.append(("label", p.caption))
                parts.append(("field", p.name))
            if has_optional(b):
                parts.append(("toggle", "‹" if b.expanded else "⋯"))
        return parts
    if k == "macro_call":
        return [("name", "run"), ("field", "@macro"), ("label", "with"), ("field", "args")]
    if k == "arguments":
        return [("name", "arguments"), ("field", "params")]
    if k == "repeat":
        return [("name", "repeat"), ("field", "count"), ("label", "times")]
    if k == "for":
        return [("name", "for each"), ("field", "var"), ("label", "in"), ("field", "iter")]
    if k == "while":
        return [("name", "while"), ("field", "cond")]
    if k == "if":
        if b.has_else and branch == b.branch_count():
            return [("name", "else")]
        return [("name", "if" if branch == 0 else "else if"), ("field", f"cond{branch}")]
    if k == "assign":
        return [("name", "set"), ("field", "name"), ("label", "to"), ("field", "value")]
    if k == "change":
        return [("name", "change"), ("field", "name"), ("field", "op"), ("field", "value")]
    if k == "comment":
        return [("field", "text")]
    if k == "raw":
        return [("field", "code")]
    return [("name", k)]


def field_display(b: Block, key: str) -> tuple[str, bool]:
    """(text, is_placeholder)."""
    if key == "@macro":
        return (b.name or "(pick a macro)"), not b.name
    if b.kind == "change" and key == "op":
        return (b.fields.get("op") or "+") + "=", False
    if b.kind == "comment":
        return "#" + b.fields.get("text", ""), False
    v = b.fields.get(key, "")
    if v.strip():
        return v, False
    if b.kind == "call":
        prim = PRIMITIVES_BY_NAME.get(b.name)
        p = next((p for p in prim.params if p.name == key), None) if prim else None
        if p is not None and p.default is not None:
            return p.default, True
        if p is not None and p.vararg:
            return "…", True
    if key == "args":
        return "…", True
    return " ", True


# ---- layout records --------------------------------------------------------

@dataclass
class FieldHit:
    rect: QRectF
    key: str
    shape: str             # pill | hex | box | text
    text: str
    placeholder: bool
    variable: bool = False


@dataclass
class LaidBlock:
    block: Block
    container: list        # the list this block lives in
    index: int
    x: float
    y: float
    w: float
    h: float
    depth: int
    rows: list = field(default_factory=list)      # [(y, h)] header rows (absolute)
    mouths: list = field(default_factory=list)    # [(x, y, w, h)] absolute
    items: list = field(default_factory=list)     # [(kind, rect, text)] drawable text parts
    fields: list = field(default_factory=list)    # [FieldHit]
    toggle: "QRectF | None" = None
    _path: "QPainterPath | None" = None
    hat: bool = False

    @property
    def rect(self) -> QRectF:
        return QRectF(self.x, self.y, self.w, self.h + NOTCH_D)

    def path(self) -> QPainterPath:
        if self._path is None:
            if self.hat:
                self._path = hat_path(self.x, self.y, self.w, self.h)
            elif self.mouths:
                sections = []
                for (ry, rh), (mx, my, mw, mh) in zip(self.rows, self.mouths):
                    sections.append((rh, mh))
                self._path = c_path(self.x, self.y, self.w, sections, self.y + self.h - (self.mouths[-1][1] + self.mouths[-1][3]))
            else:
                self._path = stmt_path(self.x, self.y, self.w, self.h)
        return self._path


@dataclass
class SnapTarget:
    x: float
    y: float
    container: list
    index: int
    kind: str = "after"    # after | mouth | top (dragged BOTTOM meets this point)


@dataclass
class StackLayout:
    blocks: list            # [LaidBlock] pre-order
    snaps: list             # [SnapTarget]
    width: float
    height: float
    tops: list = field(default_factory=list)
    prefix_bottom: list = field(default_factory=list)

    def index(self) -> None:
        self.tops = [lb.y for lb in self.blocks]
        m = float("-inf")
        self.prefix_bottom = []
        for lb in self.blocks:
            m = max(m, lb.y + lb.h + NOTCH_D)
            self.prefix_bottom.append(m)

    def visible(self, rect: QRectF):
        """LaidBlocks that may intersect rect, in paint order."""
        if not self.blocks:
            return []
        start = bisect.bisect_left(self.prefix_bottom, rect.top())
        end = bisect.bisect_right(self.tops, rect.bottom())
        return self.blocks[start:end]


# ---- layout ----------------------------------------------------------------

class Layouter:
    def __init__(self, metrics: Metrics, variables=()):
        self.m = metrics
        self.variables = set(variables)

    def _row(self, lb: LaidBlock, parts, x: float, y: float) -> tuple[float, float]:
        """Lay out one header row starting at (x, y). Returns (right_x, height)."""
        m = self.m
        b = lb.block
        items, fields_ = [], []
        cx = x + PAD_X
        row_h = MIN_H
        # raw/comment: multi-line monospace/italic text box
        if b.kind == "raw":
            lines = (b.fields.get("code") or "").split("\n")
            lh = m.line_h(m.mono)
            h = max(MIN_H, lh * len(lines) + 12)
            w = max(m.width(m.mono, ln) for ln in lines) if lines else 0
            rect = QRectF(cx, y + 6, max(40.0, w + 12), h - 12)
            lb.fields.append(FieldHit(rect, "code", "box", "\n".join(lines), False))
            return rect.right() + PAD_X, h
        for kind, payload in parts:
            if kind == "name":
                w = m.width(m.bold, payload)
                items.append(("name", QRectF(cx, y, w, row_h), payload))
                cx += w + GAP
            elif kind == "label":
                w = m.width(m.base, payload)
                items.append(("label", QRectF(cx, y, w, row_h), payload))
                cx += w + GAP
            elif kind == "trail":
                w = m.width(m.italic, payload)
                items.append(("trail", QRectF(cx, y, w, row_h), payload))
                cx += w + GAP
            elif kind == "toggle":
                w = 22.0
                r = QRectF(cx, y + (row_h - 20) / 2, w, 20)
                lb.toggle = r
                items.append(("toggle", r, payload))
                cx += w + GAP
            elif kind == "field":
                text, placeholder = field_display(b, payload)
                if b.kind == "comment":
                    w = m.width(m.italic, text)
                    rect = QRectF(cx, y + (row_h - FIELD_H) / 2, w + 4, FIELD_H)
                    fields_.append(FieldHit(rect, payload, "text", text, False))
                    cx += w + 4 + GAP
                    continue
                shape = "hex" if payload.startswith("cond") else "pill"
                font = m.italic if placeholder else m.base
                w = max(24.0, m.width(font, text) + 2 * FIELD_PAD + (8 if shape == "hex" else 0))
                if payload in ("op", "@macro") or (b.kind == "call" and _choice_like(b, payload)):
                    w += 12  # room for the ▾
                rect = QRectF(cx, y + (row_h - FIELD_H) / 2, w, FIELD_H)
                is_var = (not placeholder) and text.strip() in self.variables
                fields_.append(FieldHit(rect, payload, shape, text, placeholder, is_var))
                cx += w + GAP
        if b.trailing.strip():
            t = b.trailing.strip()
            w = m.width(m.italic, t)
            items.append(("trail", QRectF(cx, y, w, row_h), t))
            cx += w + GAP
        lb.items.extend(items)
        lb.fields.extend(fields_)
        return cx - GAP + PAD_X, row_h

    def layout_stack(self, blocks: list, x: float = 0.0, y: float = 0.0, hat_text: "str | None" = None,
                     allow_top_snap: bool = True) -> StackLayout:
        out: list[LaidBlock] = []
        snaps: list[SnapTarget] = []
        width = 0.0
        cy = y
        if hat_text is not None:
            hat = LaidBlock(Block("hat"), [], 0, x, y, 0, 0, 0, hat=True)
            text_w = self.m.width(self.m.bold, hat_text)
            hat.items.append(("name", QRectF(x + PAD_X, y + HAT_BUMP, text_w, MIN_H), hat_text))
            hat.w = max(150.0, text_w + 2 * PAD_X)
            hat.h = HAT_BUMP + MIN_H
            out.append(hat)
            cy = y + hat.h
            width = hat.w
            snaps.append(SnapTarget(x, cy, blocks, 0, "after"))
        elif allow_top_snap and blocks:
            snaps.append(SnapTarget(x, y, blocks, 0, "top"))
        end_y, w = self._layout_list(blocks, x, cy, 0, out, snaps, top_snap=False)
        width = max(width, w)
        sl = StackLayout(out, snaps, width, end_y - y)
        sl.index()
        return sl

    def _layout_list(self, blocks, x, y, depth, out, snaps, top_snap):
        cy = y
        width = 0.0
        for i, b in enumerate(blocks):
            lb = LaidBlock(b, blocks, i, x, cy, 0, 0, depth)
            out.append(lb)
            if b.kind in COMPOUND_KINDS:
                n = len(b.bodies)
                rows, mouths = [], []
                w_head = 0.0
                ry = cy
                for k in range(n):
                    rx, rh = self._row(lb, header_parts(b, k), x, ry)
                    rows.append((ry, rh))
                    w_head = max(w_head, rx - x)
                    my = ry + rh
                    snaps.append(SnapTarget(x + ARM_W, my, b.bodies[k], 0, "mouth"))
                    end, cw = self._layout_list(b.bodies[k], x + ARM_W, my, depth + 1, out, snaps, False)
                    mh = max(MOUTH_MIN, end - my)
                    mouths.append((x + ARM_W, my, cw, mh))
                    width = max(width, ARM_W + cw)
                    ry = my + mh
                lb.rows, lb.mouths = rows, mouths
                lb.w = max(160.0, w_head)
                lb.h = ry + ARM_H - cy
            else:
                rx, rh = self._row(lb, header_parts(b), x, cy)
                lb.rows = [(cy, rh)]
                lb.w = max(MIN_W, rx - x)
                lb.h = rh
            width = max(width, lb.w)
            cy += lb.h
            snaps.append(SnapTarget(x, cy, blocks, i + 1, "after"))
        return cy, width


def _choice_like(b: Block, key: str) -> bool:
    prim = PRIMITIVES_BY_NAME.get(b.name)
    p = next((p for p in prim.params if p.name == key), None) if prim else None
    return p is not None and p.kind in ("bool", "choice")


# ---- silhouettes -----------------------------------------------------------

def _top_edge(p: QPainterPath, x0: float, x1: float, y: float, notch_at: float) -> None:
    """Left->right along y, cutting the notch (an indent) at notch_at."""
    p.lineTo(notch_at, y)
    p.lineTo(notch_at + NOTCH_S, y + NOTCH_D)
    p.lineTo(notch_at + NOTCH_W - NOTCH_S, y + NOTCH_D)
    p.lineTo(notch_at + NOTCH_W, y)
    p.lineTo(x1, y)


def _bottom_edge(p: QPainterPath, x1: float, x0: float, y: float, tab_at: float) -> None:
    """Right->left along y, with the tab (a protrusion) at tab_at."""
    p.lineTo(tab_at + NOTCH_W, y)
    p.lineTo(tab_at + NOTCH_W - NOTCH_S, y + NOTCH_D)
    p.lineTo(tab_at + NOTCH_S, y + NOTCH_D)
    p.lineTo(tab_at, y)
    p.lineTo(x0, y)


def stmt_path(x: float, y: float, w: float, h: float) -> QPainterPath:
    r = RADIUS
    p = QPainterPath(QPointF(x, y + r))
    p.quadTo(x, y, x + r, y)
    _top_edge(p, x + r, x + w - r, y, x + NOTCH_X)
    p.quadTo(x + w, y, x + w, y + r)
    p.lineTo(x + w, y + h - r)
    p.quadTo(x + w, y + h, x + w - r, y + h)
    _bottom_edge(p, x + w - r, x + r, y + h, x + NOTCH_X)
    p.quadTo(x, y + h, x, y + h - r)
    p.closeSubpath()
    return p


def hat_path(x: float, y: float, w: float, h: float) -> QPainterPath:
    r = RADIUS
    p = QPainterPath(QPointF(x, y + HAT_BUMP))
    p.cubicTo(x + 25, y - 4, x + 85, y - 4, x + 110, y + HAT_BUMP - 4)
    p.lineTo(x + w - r, y + HAT_BUMP - 4)
    p.quadTo(x + w, y + HAT_BUMP - 4, x + w, y + HAT_BUMP + r - 4)
    p.lineTo(x + w, y + h - r)
    p.quadTo(x + w, y + h, x + w - r, y + h)
    _bottom_edge(p, x + w - r, x + r, y + h, x + NOTCH_X)
    p.quadTo(x, y + h, x, y + h - r)
    p.closeSubpath()
    return p


def c_path(x: float, y: float, w: float, sections: list, arm_h: float) -> QPainterPath:
    """sections: [(header_h, mouth_h)] top to bottom; arm_h: bottom arm."""
    r = RADIUS
    xa = x + ARM_W
    p = QPainterPath(QPointF(x, y + r))
    p.quadTo(x, y, x + r, y)
    _top_edge(p, x + r, x + w - r, y, x + NOTCH_X)
    p.quadTo(x + w, y, x + w, y + r)
    cy = y
    for k, (hh, mh) in enumerate(sections):
        head_bottom = cy + hh
        p.lineTo(x + w, head_bottom - r)
        p.quadTo(x + w, head_bottom, x + w - r, head_bottom)
        _bottom_edge(p, x + w - r, xa + r, head_bottom, xa + NOTCH_X)
        p.quadTo(xa, head_bottom, xa, head_bottom + r)
        mouth_bottom = head_bottom + mh
        p.lineTo(xa, mouth_bottom - r)
        p.quadTo(xa, mouth_bottom, xa + r, mouth_bottom)
        _top_edge(p, xa + r, x + w - r, mouth_bottom, xa + NOTCH_X)
        p.quadTo(x + w, mouth_bottom, x + w, mouth_bottom + r)
        cy = mouth_bottom
    bottom = cy + arm_h
    p.lineTo(x + w, bottom - r)
    p.quadTo(x + w, bottom, x + w - r, bottom)
    _bottom_edge(p, x + w - r, x + r, bottom, x + NOTCH_X)
    p.quadTo(x, bottom, x, bottom - r)
    p.closeSubpath()
    return p


def hex_path(r: QRectF) -> QPainterPath:
    s = r.height() / 2
    p = QPainterPath(QPointF(r.left(), r.center().y()))
    p.lineTo(r.left() + s, r.top())
    p.lineTo(r.right() - s, r.top())
    p.lineTo(r.right(), r.center().y())
    p.lineTo(r.right() - s, r.bottom())
    p.lineTo(r.left() + s, r.bottom())
    p.closeSubpath()
    return p


# ---- painting --------------------------------------------------------------

def pill_fill(theme: Theme) -> QColor:
    """Socket fill: the theme's text color (light on the default dark
    theme), so sockets read as "type here" without a hardcoded hex."""
    return theme.text()


def pill_text(theme: Theme) -> QColor:
    return contrast_text(pill_fill(theme))


def paint_block(p: QPainter, lb: LaidBlock, m: Metrics, theme: Theme, selected: bool = False,
                hover_field: "str | None" = None, drop_field: "str | None" = None) -> None:
    b = lb.block
    color = theme.input_color() if lb.hat else block_color(b, theme)
    path = lb.path()
    p.setPen(QPen(color.darker(145), 1.2))
    p.setBrush(color)
    p.drawPath(path)
    if selected:
        p.setPen(QPen(theme.text(), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    ink = contrast_text(color)
    for kind, rect, text in lb.items:
        if kind == "name":
            p.setFont(m.bold)
            p.setPen(ink)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        elif kind == "label":
            p.setFont(m.base)
            p.setPen(ink)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        elif kind == "trail":
            p.setFont(m.italic)
            c = QColor(ink)
            c.setAlpha(170)
            p.setPen(c)
            p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        elif kind == "toggle":
            p.setPen(Qt.NoPen)
            p.setBrush(color.darker(125))
            p.drawRoundedRect(rect, 6, 6)
            p.setFont(m.bold)
            p.setPen(ink)
            p.drawText(rect, Qt.AlignCenter, text)
    for fh in lb.fields:
        _paint_field(p, b, fh, m, theme, color, ink, drop=(fh.key == drop_field))


def _paint_field(p: QPainter, b: Block, fh: FieldHit, m: Metrics, theme: Theme, color: QColor, ink: QColor,
                 drop: bool = False) -> None:
    r = fh.rect
    if fh.shape == "text":      # comment text, drawn straight on the block
        p.setFont(m.italic)
        p.setPen(ink)
        p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, fh.text)
        return
    if fh.shape == "box":       # raw code
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(r, 4, 4)
        p.setFont(m.mono)
        p.setPen(contrast_text(color.darker(140)))
        p.drawText(r.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, fh.text)
        return
    if fh.variable or fh.shape == "hex":
        fill = theme.input_color()
        if fh.placeholder:
            fill = fill.darker(130)
        text_c = contrast_text(fill)
    elif fh.placeholder:
        fill = color.darker(122)
        text_c = QColor(ink)
        text_c.setAlpha(150)
    else:
        fill = pill_fill(theme)
        text_c = pill_text(theme)
    path = hex_path(r) if fh.shape == "hex" else None
    p.setPen(QPen(theme.text() if drop else fill.darker(150), 2.4 if drop else 1.0))
    p.setBrush(fill)
    if path is not None:
        p.drawPath(path)
    else:
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
    p.setFont(m.italic if fh.placeholder else m.base)
    p.setPen(text_c)
    inner = r.adjusted(FIELD_PAD + (4 if fh.shape == "hex" else 0), 0, -FIELD_PAD, 0)
    arrow = fh.key in ("op", "@macro") or (b.kind == "call" and _choice_like(b, fh.key))
    if arrow:
        inner = inner.adjusted(0, 0, -10, 0)
    p.drawText(inner, Qt.AlignVCenter | Qt.AlignLeft, fh.text)
    if arrow:
        p.drawText(QRectF(inner.right(), r.top(), 12, r.height()), Qt.AlignCenter, "▾")


def paint_ghost(p: QPainter, x: float, y: float, w: float, h: float, theme: Theme, bottom: bool = False) -> None:
    """The snap preview: a translucent silhouette where the drop will land."""
    path = stmt_path(x, y - h if bottom else y, w, h)
    c = QColor(theme.text())
    c.setAlpha(60)
    p.setBrush(c)
    p.setPen(QPen(theme.text(), 2.0, Qt.DashLine))
    p.drawPath(path)
