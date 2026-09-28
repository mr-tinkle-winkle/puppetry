"""
Block geometry + painting for the block editor: puzzle-piece silhouettes
(a notch cut into every top edge, a matching tab under every bottom edge,
C-shaped mouths for loops/ifs, hats for "when ... pressed" and "create
function"), sockets (text pills, reporters nested inside each other, list
sockets that always offer one more empty slot), and free-floating notes.

Layout only computes numbers; QPainterPaths are built lazily the first
time a block is painted or hit-tested, so a 20k-line transcribed macro
lays out without building 20k paths.

Socket addressing: a FieldHit's `ref` is (key, index, path) --
  key   = the block field ("time_", "cond0", "keys", "@name" ...)
  index = position in a list socket (None for a plain socket; the "+"
          slot at the end has index == len(list))
  path  = tuple of Rep field names walking into nested reporters,
          e.g. ("left",) for the left side of a comparison.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from block_model import COMPARE_OPS, COMPOUND_KINDS, Block, Rep, expr_text, rep_label
from reference import PRIMITIVES_BY_NAME
from ui_kit.theme import Theme, contrast_text

# ---- geometry (scene units) -------------------------------------------------
PAD_X = 10.0
MIN_H = 34.0
MIN_W = 70.0
NOTCH_X = 14.0
NOTCH_W = 26.0
NOTCH_S = 6.0
NOTCH_D = 6.0
RADIUS = 5.0
ARM_W = 18.0
MOUTH_MIN = 26.0
ARM_H = 22.0
HAT_BUMP = 16.0
FIELD_H = 24.0
FIELD_PAD = 8.0
GAP = 6.0
NEST_PAD = 4.0
SNAP_DIST = 30.0

CMP_LABEL = dict(COMPARE_OPS)
REP_KINDS_CONTAINER = ("compare", "held", "press")


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


# ---- colors ----------------------------------------------------------------

def block_category(b: Block) -> str:
    if b.kind == "call":
        prim = PRIMITIVES_BY_NAME.get(b.name)
        return prim.category if prim else "neutral"
    if b.kind == "arguments":
        return "input"
    if b.kind in ("assign", "change", "macro_call"):
        return "output"
    return "neutral"


def block_color(b: Block, theme: Theme, customs: "dict | None" = None) -> QColor:
    if b.kind == "comment":
        return theme.neutral_block_color().darker(150)
    if b.kind == "raw":
        return theme.custom_color()
    if b.kind in ("def", "func_call", "return"):
        return theme.function_color()
    if b.kind == "custom":
        d = (customs or {}).get(b.name)
        c = QColor(d.color) if d and QColor.isValidColorName(d.color) else theme.custom_color()
        return c
    if b.kind in COMPOUND_KINDS:
        return theme.neutral_block_color().darker(108)
    return theme.category_color(block_category(b))


def rep_color(r: Rep, theme: Theme) -> QColor:
    if r.kind == "bool":
        return theme.true_color() if r.name == "True" else theme.false_color()
    return theme.input_color()


# ---- block content ---------------------------------------------------------

def has_optional(b: Block) -> bool:
    prim = PRIMITIVES_BY_NAME.get(b.name) if b.kind == "call" else None
    return bool(prim) and any(not p.required and not p.vararg for p in prim.params)


def _filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return bool(v)
    return True


def header_parts(b: Block, branch: int = 0, customs: "dict | None" = None) -> list:
    """[(kind, payload)] -- kind: name | label | sock | list | pick | toggle."""
    f = b.fields
    k = b.kind
    if k == "call":
        prim = PRIMITIVES_BY_NAME.get(b.name)
        parts = [("name", prim.title if prim else b.name)]
        if prim:
            for p in prim.params:
                shown = p.required or p.vararg or b.expanded or _filled(f.get(p.name))
                if not shown:
                    continue
                if p.caption:
                    parts.append(("label", p.caption))
                parts.append(("list" if p.vararg else "sock", p.name))
            if has_optional(b):
                parts.append(("toggle", "‹" if b.expanded else "⋯"))
        return parts
    if k == "macro_call":
        return [("name", "run macro"), ("pick", "@name"), ("label", "with arguments"), ("list", "args")]
    if k == "func_call":
        return [("name", "run function"), ("pick", "@name"), ("label", "with arguments"), ("list", "args")]
    if k == "custom":
        d = (customs or {}).get(b.name)
        parts = [("name", d.name if d else b.name)]
        for a in (d.args if d else []):
            parts.append(("label", a.name))
            parts.append(("sock", a.name))
        if d is None:
            parts.append(("label", "(custom block not found)"))
        return parts
    if k == "arguments":
        return [("name", "arguments"), ("list", "params")]
    if k == "repeat":
        return [("name", "repeat"), ("sock", "count"), ("label", "times")]
    if k == "for":
        return [("name", "for each"), ("namesock", "var"), ("label", "in"), ("sock", "iter")]
    if k == "while":
        return [("name", "while"), ("sock", "cond")]
    if k == "if":
        if b.has_else and branch == b.branch_count():
            return [("name", "else")]
        return [("name", "if" if branch == 0 else "else if"), ("sock", f"cond{branch}")]
    if k == "assign":
        return [("name", "set"), ("namesock", "name"), ("label", "to"), ("sock", "value")]
    if k == "change":
        return [("name", "change"), ("namesock", "name"), ("pick", "op"), ("sock", "value")]
    if k == "return":
        return [("name", "return"), ("sock", "value")]
    if k == "def":
        return [("name", "create function"), ("namesock", "@defname"), ("label", "with"), ("list", "params")]
    if k == "comment":
        return [("note", "text")]
    if k == "raw":
        return [("code", "code")]
    return [("name", k)]


LIST_NAME_KEYS = {("arguments", "params"), ("def", "params")}   # list elements are names, not values


def param_default(b: Block, key: str, customs=None):
    if b.kind == "call":
        prim = PRIMITIVES_BY_NAME.get(b.name)
        p = next((p for p in prim.params if p.name == key), None) if prim else None
        return p
    return None


# ---- layout records --------------------------------------------------------

@dataclass
class FieldHit:
    rect: QRectF
    ref: tuple             # (key, index, path)
    kind: str              # text | name | pick | choice | append | code | note | var | bool | mouse | buttons |
                           # compare | held | press | cmpop
    text: str
    placeholder: bool = False
    accepts: bool = True   # can a reporter be dropped here?
    rep: "Rep | None" = None
    shape: str = "pill"    # pill | hex | box | text

    @property
    def key(self) -> str:
        return self.ref[0]


@dataclass
class LaidBlock:
    block: Block
    container: list
    index: int
    x: float
    y: float
    w: float
    h: float
    depth: int
    rows: list = field(default_factory=list)
    mouths: list = field(default_factory=list)
    items: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    toggle: "QRectF | None" = None
    _path: "QPainterPath | None" = None
    hat: str = ""          # "" | "main" | "def"

    @property
    def rect(self) -> QRectF:
        top = self.y - (HAT_BUMP if self.hat else 0)
        return QRectF(self.x, top, self.w, self.h + NOTCH_D + (HAT_BUMP if self.hat else 0))

    def path(self) -> QPainterPath:
        if self._path is None:
            if self.hat:
                self._path = hat_path(self.x, self.y, self.w, self.h)
            elif self.mouths:
                sections = [(rh, mh) for (ry, rh), (mx, my, mw, mh) in zip(self.rows, self.mouths)]
                self._path = c_path(self.x, self.y, self.w, sections,
                                    self.y + self.h - (self.mouths[-1][1] + self.mouths[-1][3]))
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
    blocks: list
    snaps: list
    width: float
    height: float
    tops: list = field(default_factory=list)
    prefix_bottom: list = field(default_factory=list)

    def index(self) -> None:
        self.tops = [lb.rect.top() for lb in self.blocks]
        m = float("-inf")
        self.prefix_bottom = []
        for lb in self.blocks:
            m = max(m, lb.rect.bottom())
            self.prefix_bottom.append(m)

    def visible(self, rect: QRectF):
        if not self.blocks:
            return []
        start = bisect.bisect_left(self.prefix_bottom, rect.top())
        end = bisect.bisect_right(self.tops, rect.bottom())
        return self.blocks[start:end]


# ---- layout ----------------------------------------------------------------

class Layouter:
    def __init__(self, metrics: Metrics, variables=(), customs=None, macro_names=(), functions=()):
        self.m = metrics
        self.variables = set(variables)
        self.customs = customs or {}

    # -- socket values (recursive) --------------------------------------------
    def _text_w(self, text: str, placeholder: bool) -> float:
        return self.m.width(self.m.italic if placeholder else self.m.base, text)

    def measure(self, v, kind_hint: str = "text") -> tuple[float, float]:
        m = self.m
        if isinstance(v, Rep):
            if v.kind in REP_KINDS_CONTAINER:
                w, h = 0.0, FIELD_H
                for part in self._rep_parts(v):
                    pw, ph = self._measure_part(part)
                    w += pw + GAP
                    h = max(h, ph + 2 * NEST_PAD)
                pad = h / 2 if v.kind in ("compare", "held") else FIELD_PAD
                return w - GAP + 2 * pad, h
            label = rep_label(v) + (" ▾" if v.kind in ("mouse", "var") else "")
            return max(28.0, m.width(m.base, label) + 2 * FIELD_PAD), FIELD_H
        text = expr_text(v) if not isinstance(v, str) else v
        return max(24.0, m.width(m.base, text) + 2 * FIELD_PAD + (12 if kind_hint == "choice" else 0)), FIELD_H

    def _rep_parts(self, r: Rep) -> list:
        if r.kind == "compare":
            return [("sock", "left"), ("label", "is"), ("cmpop", r.name), ("sock", "right")]
        if r.kind == "held":
            return [("sock", "key"), ("label", "is held")]
        if r.kind == "press":
            return [("label", "key pressed"), ("label", "on"), ("sock", "button"), ("label", "block it"),
                    ("sock", "repress")]
        return []

    def _measure_part(self, part) -> tuple[float, float]:
        kind, payload = part
        if kind == "label":
            return self.m.width(self.m.base, payload), FIELD_H
        if kind == "cmpop":
            return self.m.width(self.m.base, CMP_LABEL.get(payload, payload) + " ▾") + 2 * FIELD_PAD, FIELD_H
        return (0.0, FIELD_H)   # sockets measured by caller with their value

    def place_value(self, lb: LaidBlock, v, ref: tuple, x: float, cy: float, kind: str = "text",
                    placeholder_text: str = "", accepts: bool = True) -> float:
        """Lay out a socket value with its left edge at x, vertically centered
        on cy. Returns its width."""
        m = self.m
        if isinstance(v, Rep):
            w, h = self.measure_rep(v)
            rect = QRectF(x, cy - h / 2, w, h)
            if v.kind in REP_KINDS_CONTAINER:
                shape = "hex" if v.kind in ("compare", "held") else "pill"
                lb.fields.append(FieldHit(rect, ref, v.kind, "", rep=v, shape=shape, accepts=True))
                pad = h / 2 if shape == "hex" else FIELD_PAD
                cx = x + pad
                for pk, payload in self._rep_parts(v):
                    if pk == "label":
                        tw = m.width(m.base, payload)
                        lb.items.append(("replabel", QRectF(cx, cy - FIELD_H / 2, tw, FIELD_H), payload))
                        cx += tw + GAP
                    elif pk == "cmpop":
                        text = CMP_LABEL.get(payload, payload)
                        tw = m.width(m.base, text + " ▾") + 2 * FIELD_PAD
                        lb.fields.append(FieldHit(QRectF(cx, cy - FIELD_H / 2, tw, FIELD_H), ref[:2] + (ref[2],),
                                                  "cmpop", text, accepts=False, rep=v))
                        cx += tw + GAP
                    else:
                        sub = v.fields.get(payload, "")
                        ph = "any" if (v.kind == "press" and payload == "button") else ""
                        cx += self.place_value(lb, sub, (ref[0], ref[1], ref[2] + (payload,)), cx, cy,
                                               placeholder_text=ph) + GAP
                return w
            label = rep_label(v) + (" ▾" if v.kind in ("mouse", "var") else "")
            lb.fields.append(FieldHit(rect, ref, v.kind, label, rep=v, accepts=True))
            return w
        text = v if isinstance(v, str) else ""
        placeholder = not text.strip()
        shown = text if not placeholder else (placeholder_text or " ")
        w = max(24.0, self._text_w(shown, placeholder) + 2 * FIELD_PAD + (12 if kind in ("choice", "pick") else 0))
        rect = QRectF(x, cy - FIELD_H / 2, w, FIELD_H)
        lb.fields.append(FieldHit(rect, ref, kind, shown, placeholder, accepts=accepts))
        return w

    def measure_rep(self, r: Rep) -> tuple[float, float]:
        if r.kind not in REP_KINDS_CONTAINER:
            return self.measure(r)
        w, h = 0.0, FIELD_H
        for pk, payload in self._rep_parts(r):
            if pk in ("label", "cmpop"):
                pw, ph = self._measure_part((pk, payload))
            else:
                sub = r.fields.get(payload, "")
                if isinstance(sub, Rep):
                    pw, ph = self.measure_rep(sub)
                else:
                    ph_text = "any" if (r.kind == "press" and payload == "button") else ""
                    t = sub if isinstance(sub, str) and sub.strip() else (ph_text or " ")
                    pw, ph = max(24.0, self._text_w(t, not (isinstance(sub, str) and sub.strip())) + 2 * FIELD_PAD), FIELD_H
            w += pw + GAP
            h = max(h, ph + 2 * NEST_PAD)
        pad = h / 2 if r.kind in ("compare", "held") else FIELD_PAD
        return w - GAP + 2 * pad, h

    def value_size(self, v, kind: str = "text", placeholder_text: str = "") -> tuple[float, float]:
        if isinstance(v, Rep):
            return self.measure_rep(v)
        text = v if isinstance(v, str) else ""
        ph = not text.strip()
        shown = text if not ph else (placeholder_text or " ")
        return max(24.0, self._text_w(shown, ph) + 2 * FIELD_PAD + (12 if kind in ("choice", "pick") else 0)), FIELD_H

    # -- one header row ------------------------------------------------------
    def _socket_kind(self, b: Block, key: str) -> tuple[str, str, bool]:
        """(hit kind, placeholder text, accepts reporters)."""
        if b.kind == "call":
            prim = PRIMITIVES_BY_NAME.get(b.name)
            p = next((p for p in prim.params if p.name == key), None) if prim else None
            if p is not None:
                ph = p.default.strip('"') if p.default is not None else ""
                if p.kind in ("bool", "choice"):
                    return "choice", ph, True
                return "text", ph, True
        if b.kind == "custom":
            d = self.customs.get(b.name)
            a = next((a for a in d.args if a.name == key), None) if d else None
            if a is not None and a.source in ("mouse", "list"):
                return "choice", a.default, True
            return "text", (a.default if a else ""), True
        if key in ("cond", "count", "value", "iter") or key.startswith("cond"):
            return "text", "", True
        return "text", "", True

    def _row(self, lb: LaidBlock, parts, x: float, y: float) -> tuple[float, float]:
        m = self.m
        b = lb.block
        if b.kind == "raw":
            lines = (b.fields.get("code") or "").split("\n")
            lh = m.line_h(m.mono)
            h = max(MIN_H, lh * len(lines) + 12)
            w = max(m.width(m.mono, ln) for ln in lines) if lines else 0
            rect = QRectF(x + PAD_X, y + 6, max(40.0, w + 12), h - 12)
            lb.fields.append(FieldHit(rect, ("code", None, ()), "code", "\n".join(lines), accepts=False, shape="box"))
            return rect.right() + PAD_X, h
        # measure row height first (nested reporters make rows taller)
        row_h = MIN_H
        for kind, payload in parts:
            for v, sk in self._part_values(b, kind, payload):
                _w, h = self.value_size(v, sk)
                row_h = max(row_h, h + 10)
        cy = y + row_h / 2
        cx = x + PAD_X
        for kind, payload in parts:
            if kind == "name":
                w = m.width(m.bold, payload)
                lb.items.append(("name", QRectF(cx, y, w, row_h), payload))
                cx += w + GAP
            elif kind == "label":
                w = m.width(m.base, payload)
                lb.items.append(("label", QRectF(cx, y, w, row_h), payload))
                cx += w + GAP
            elif kind == "toggle":
                r = QRectF(cx, cy - 10, 22.0, 20)
                lb.toggle = r
                lb.items.append(("toggle", r, payload))
                cx += 22.0 + GAP
            elif kind == "note":
                text = "#" + b.fields.get("text", "")
                w = m.width(m.italic, text)
                rect = QRectF(cx, cy - FIELD_H / 2, w + 4, FIELD_H)
                lb.fields.append(FieldHit(rect, ("text", None, ()), "note", text, accepts=False, shape="text"))
                cx += w + 4 + GAP
            elif kind == "pick":
                if payload == "@name":
                    text, ph = (b.name, False) if b.name else ("(pick one)", True)
                else:  # change op
                    text, ph = (b.fields.get("op") or "+") + "=", False
                w = self._text_w(text + " ▾", ph) + 2 * FIELD_PAD
                rect = QRectF(cx, cy - FIELD_H / 2, w, FIELD_H)
                lb.fields.append(FieldHit(rect, (payload, None, ()), "pick", text + " ▾", ph, accepts=False))
                cx += w + GAP
            elif kind == "namesock":
                if payload == "@defname":
                    v = b.name
                else:
                    v = b.fields.get(payload, "")
                w = self.place_value(lb, v if isinstance(v, str) else expr_text(v), (payload, None, ()), cx, cy,
                                     "name", "name", accepts=False)
                cx += w + GAP
            elif kind == "sock":
                sk, ph, acc = self._socket_kind(b, payload)
                v = b.fields.get(payload, "")
                cx += self.place_value(lb, v, (payload, None, ()), cx, cy, sk, ph, acc) + GAP
            elif kind == "list":
                items = b.fields.get(payload) or []
                if not isinstance(items, list):
                    items = [items]
                names = (b.kind, payload) in LIST_NAME_KEYS
                for i, v in enumerate(items):
                    cx += self.place_value(lb, v, (payload, i, ()), cx, cy, "name" if names else "text", "",
                                           accepts=not names) + 4
                w = 24.0
                rect = QRectF(cx, cy - FIELD_H / 2, w, FIELD_H)
                lb.fields.append(FieldHit(rect, (payload, len(items), ()), "append", "+", True, accepts=not names))
                cx += w + GAP
        if b.trailing.strip():
            t = b.trailing.strip()
            w = m.width(m.italic, t)
            lb.items.append(("trail", QRectF(cx, y, w, row_h), t))
            cx += w + GAP
        return cx - GAP + PAD_X, row_h

    def _part_values(self, b: Block, kind: str, payload: str):
        if kind == "sock":
            yield b.fields.get(payload, ""), "text"
        elif kind == "list":
            items = b.fields.get(payload) or []
            for v in (items if isinstance(items, list) else [items]):
                yield v, "text"

    # -- stacks ----------------------------------------------------------------
    def layout_stack(self, blocks: list, x: float = 0.0, y: float = 0.0, hat=None,
                     allow_top_snap: bool = True) -> StackLayout:
        """hat: None, a str (the main "when ... pressed" hat), or a def Block
        (a function's "create function" hat; `blocks` is its body)."""
        out: list[LaidBlock] = []
        snaps: list[SnapTarget] = []
        width = 0.0
        cy = y
        if hat is not None:
            if isinstance(hat, str):
                lb = LaidBlock(Block("hat"), [], 0, x, y, 0, 0, 0, hat="main")
                text_w = self.m.width(self.m.bold, hat)
                lb.items.append(("name", QRectF(x + PAD_X, y, text_w, MIN_H), hat))
                lb.w = max(150.0, text_w + 2 * PAD_X)
                lb.h = MIN_H
            else:
                lb = LaidBlock(hat, [], 0, x, y, 0, 0, 0, hat="def")
                rx, rh = self._row(lb, header_parts(hat), x, y)
                lb.w = max(170.0, rx - x)
                lb.h = rh
            lb.rows = [(y, lb.h)]
            out.append(lb)
            cy = y + lb.h
            width = lb.w
            snaps.append(SnapTarget(x, cy, blocks, 0, "after"))
        elif allow_top_snap and blocks:
            snaps.append(SnapTarget(x, y, blocks, 0, "top"))
        end_y, w = self._layout_list(blocks, x, cy, 0, out, snaps)
        width = max(width, w)
        top = y - (HAT_BUMP if hat is not None else 0)
        sl = StackLayout(out, snaps, width, end_y - top)
        sl.index()
        return sl

    def _layout_list(self, blocks, x, y, depth, out, snaps):
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
                    rx, rh = self._row(lb, header_parts(b, k, self.customs), x, ry)
                    rows.append((ry, rh))
                    w_head = max(w_head, rx - x)
                    my = ry + rh
                    snaps.append(SnapTarget(x + ARM_W, my, b.bodies[k], 0, "mouth"))
                    end, cw = self._layout_list(b.bodies[k], x + ARM_W, my, depth + 1, out, snaps)
                    mh = max(MOUTH_MIN, end - my)
                    mouths.append((x + ARM_W, my, cw, mh))
                    width = max(width, ARM_W + cw)
                    ry = my + mh
                lb.rows, lb.mouths = rows, mouths
                lb.w = max(160.0, w_head)
                lb.h = ry + ARM_H - cy
            else:
                rx, rh = self._row(lb, header_parts(b, 0, self.customs), x, cy)
                lb.rows = [(cy, rh)]
                lb.w = max(MIN_W, rx - x)
                lb.h = rh
            width = max(width, lb.w)
            cy += lb.h
            snaps.append(SnapTarget(x, cy, blocks, i + 1, "after"))
        return cy, width

    # -- a lone reporter (palette pixmaps, drag previews) ----------------------
    def layout_reporter(self, r: Rep) -> LaidBlock:
        lb = LaidBlock(Block("reporter"), [], 0, 0, 0, 0, 0, 0)
        w, h = self.measure_rep(r)
        self.place_value(lb, r, ("value", None, ()), 0, h / 2)
        lb.w, lb.h = w, h
        return lb


# ---- silhouettes -----------------------------------------------------------

def _top_edge(p: QPainterPath, x0: float, x1: float, y: float, notch_at: float) -> None:
    p.lineTo(notch_at, y)
    p.lineTo(notch_at + NOTCH_S, y + NOTCH_D)
    p.lineTo(notch_at + NOTCH_W - NOTCH_S, y + NOTCH_D)
    p.lineTo(notch_at + NOTCH_W, y)
    p.lineTo(x1, y)


def _bottom_edge(p: QPainterPath, x1: float, x0: float, y: float, tab_at: float) -> None:
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


def note_path(x: float, y: float, w: float, h: float) -> QPainterPath:
    """A free-floating note: a plain rounded card with a folded corner --
    no notch, no tab, so it visibly doesn't snap to anything."""
    p = QPainterPath()
    p.addRoundedRect(QRectF(x, y, w, h), 6, 6)
    return p


def hat_path(x: float, y: float, w: float, h: float) -> QPainterPath:
    """Hat: the bump rises ABOVE y (y is where the content row starts)."""
    r = RADIUS
    top = y - HAT_BUMP
    p = QPainterPath(QPointF(x, y))
    p.cubicTo(x + 25, top - 4, x + 85, top - 4, x + 110, y - 4)
    p.lineTo(x + w - r, y - 4)
    p.quadTo(x + w, y - 4, x + w, y + r - 4)
    p.lineTo(x + w, y + h - r)
    p.quadTo(x + w, y + h, x + w - r, y + h)
    _bottom_edge(p, x + w - r, x + r, y + h, x + NOTCH_X)
    p.quadTo(x, y + h, x, y + h - r)
    p.closeSubpath()
    return p


def c_path(x: float, y: float, w: float, sections: list, arm_h: float) -> QPainterPath:
    r = RADIUS
    xa = x + ARM_W
    p = QPainterPath(QPointF(x, y + r))
    p.quadTo(x, y, x + r, y)
    _top_edge(p, x + r, x + w - r, y, x + NOTCH_X)
    p.quadTo(x + w, y, x + w, y + r)
    cy = y
    for hh, mh in sections:
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
                drop_ref: "tuple | None" = None, customs: "dict | None" = None) -> None:
    b = lb.block
    if lb.hat == "main":
        color = theme.input_color()
    elif b.kind == "reporter":
        color = None
    else:
        color = block_color(b, theme, customs)
    if color is not None:
        path = lb.path()
        p.setPen(QPen(color.darker(145), 1.2))
        p.setBrush(color)
        p.drawPath(path)
        if selected:
            p.setPen(QPen(theme.text(), 2.2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        ink = contrast_text(color)
    else:
        color = theme.input_color()
        ink = contrast_text(color)
    for kind, rect, text in lb.items:
        if kind in ("name", "label"):
            p.setFont(m.bold if kind == "name" else m.base)
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
    # fields in order: containers before their children, so children paint on top
    labels_after = [it for it in lb.items if it[0] == "replabel"]
    for fh in lb.fields:
        _paint_field(p, fh, m, theme, color, ink, drop=(drop_ref is not None and fh.ref == drop_ref))
        # a container's own labels ("is", "is held") paint right after it
        if fh.rep is not None and fh.kind in REP_KINDS_CONTAINER:
            c = contrast_text(rep_color(fh.rep, theme))
            for _k, rect, text in labels_after:
                if fh.rect.contains(rect.center()):
                    p.setFont(m.base)
                    p.setPen(c)
                    p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, text)


def _paint_field(p: QPainter, fh: FieldHit, m: Metrics, theme: Theme, color: QColor, ink: QColor,
                 drop: bool = False) -> None:
    r = fh.rect
    if fh.kind == "note":
        p.setFont(m.italic)
        p.setPen(ink)
        p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, fh.text)
        return
    if fh.kind == "code":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(r, 4, 4)
        p.setFont(m.mono)
        p.setPen(contrast_text(color.darker(140)))
        p.drawText(r.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, fh.text)
        return
    if fh.rep is not None and fh.kind != "cmpop":
        fill = rep_color(fh.rep, theme)
        text_c = contrast_text(fill)
        outline = QPen(theme.text() if drop else fill.darker(150), 2.4 if drop else 1.0)
        p.setPen(outline)
        p.setBrush(fill if fh.kind not in REP_KINDS_CONTAINER else fill.darker(112))
        if fh.shape == "hex":
            p.drawPath(hex_path(r))
        else:
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if fh.kind not in REP_KINDS_CONTAINER:
            p.setFont(m.base)
            p.setPen(text_c)
            p.drawText(r.adjusted(FIELD_PAD, 0, -FIELD_PAD + 2, 0), Qt.AlignVCenter | Qt.AlignLeft, fh.text)
        return
    if fh.kind == "cmpop":
        fill = theme.input_color().lighter(118)
        p.setPen(QPen(fill.darker(150), 1.0))
        p.setBrush(fill)
        p.drawRoundedRect(r, 5, 5)
        p.setFont(m.base)
        p.setPen(contrast_text(fill))
        p.drawText(r.adjusted(FIELD_PAD, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, fh.text + " ▾")
        return
    if fh.kind == "append":
        c = QColor(ink)
        c.setAlpha(120)
        p.setPen(QPen(c, 1.2, Qt.DashLine))
        p.setBrush(QColor(0, 0, 0, 0) if not drop else QColor(255, 255, 255, 60))
        if drop:
            p.setPen(QPen(theme.text(), 2.4))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(m.bold)
        p.setPen(c)
        p.drawText(r, Qt.AlignCenter, "+")
        return
    if fh.placeholder:
        fill = color.darker(122)
        text_c = QColor(ink)
        text_c.setAlpha(150)
    else:
        fill = pill_fill(theme)
        text_c = pill_text(theme)
    if fh.kind == "pick":
        fill = color.darker(118)
        text_c = ink
    p.setPen(QPen(theme.text() if drop else fill.darker(150), 2.4 if drop else 1.0))
    p.setBrush(fill)
    if fh.kind == "name":
        p.drawRoundedRect(r, 5, 5)
    else:
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
    p.setFont(m.italic if fh.placeholder else m.base)
    p.setPen(text_c)
    inner = r.adjusted(FIELD_PAD, 0, -FIELD_PAD, 0)
    if fh.kind == "choice":
        inner = inner.adjusted(0, 0, -10, 0)
    p.drawText(inner, Qt.AlignVCenter | Qt.AlignLeft, fh.text)
    if fh.kind == "choice":
        p.drawText(QRectF(inner.right(), r.top(), 12, r.height()), Qt.AlignCenter, "▾")


def paint_ghost(p: QPainter, x: float, y: float, w: float, h: float, theme: Theme, bottom: bool = False) -> None:
    path = stmt_path(x, y - h if bottom else y, w, h)
    c = QColor(theme.text())
    c.setAlpha(60)
    p.setBrush(c)
    p.setPen(QPen(theme.text(), 2.0, Qt.DashLine))
    p.drawPath(path)


def paint_note(p: QPainter, rect: QRectF, text: str, m: Metrics, theme: Theme, selected: bool = False) -> None:
    fill = theme.neutral_block_color().darker(150)
    p.setPen(QPen(fill.darker(140), 1.2))
    p.setBrush(fill)
    p.drawPath(note_path(rect.x(), rect.y(), rect.width(), rect.height()))
    # folded corner
    c = QColor(fill.lighter(140))
    fold = QPainterPath(QPointF(rect.right() - 12, rect.top()))
    fold.lineTo(rect.right(), rect.top() + 12)
    fold.lineTo(rect.right() - 12, rect.top() + 12)
    fold.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPath(fold)
    if selected:
        p.setPen(QPen(theme.text(), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, 6, 6)
    p.setFont(m.italic)
    p.setPen(contrast_text(fill))
    p.drawText(rect.adjusted(10, 0, -14, 0), Qt.AlignVCenter | Qt.AlignLeft, text)


def note_size(text: str, m: Metrics) -> tuple[float, float]:
    return max(90.0, m.width(m.italic, text) + 26), MIN_H
