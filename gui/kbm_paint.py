"""
Draws the keyboard + mouse (kbm_layout) with QPainter. Used by the Input
Visualizer, the Dictionary's key-name map, the overlay settings preview and
the overlay video renderer. The browser pages (overlay_web/kbm.js) draw the
same thing from the same layout JSON and style keys.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

import kbm_layout as kl
from overlay_config import parse_color, schema_defaults, STYLE_SCHEMA


def qcolor(s: str) -> QColor:
    r, g, b, a = parse_color(s)
    return QColor(r, g, b, a)


def _hex(c: QColor) -> str:
    return "#{:02x}{:02x}{:02x}{:02x}".format(c.red(), c.green(), c.blue(), c.alpha())


def theme_style(theme) -> dict:
    """The overlay style, recolored with the app's theme (for the in-app
    visualizer and the Dictionary)."""
    s = schema_defaults(STYLE_SCHEMA)
    surf = theme.surface()
    key = surf.lighter(140) if surf.lightness() < 128 else surf.darker(108)
    s.update(background="#00000000", key_color=_hex(key), key_outline=_hex(key.darker(130)),
             pressed_color=_hex(theme.input_color()), macro_color=_hex(theme.output_color()),
             text_color=_hex(theme.text()), pressed_text_color="#111111ff",
             arrow_color=_hex(theme.input_color()), glow=False, release_fade_ms=0, font_family="",
             show_macro_output=True)
    return s


def _blend(a: QColor, b: QColor, f: float) -> QColor:
    f = max(0.0, min(1.0, f))
    return QColor(round(a.red() + (b.red() - a.red()) * f), round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f), round(a.alpha() + (b.alpha() - a.alpha()) * f))


def _contrast(c: QColor) -> QColor:
    return QColor("#111111") if c.lightness() > 150 else QColor("#f2f2f2")


class _Fonts:
    """Cached fonts + metrics; fitting text per key is the hot part."""

    def __init__(self, family: str, bold: bool):
        self.family = family
        self.bold = bold
        self._cache: dict = {}

    def get(self, px: int):
        px = max(4, int(px))
        f = self._cache.get(px)
        if f is None:
            font = QFont(self.family) if self.family else QFont()
            font.setPixelSize(px)
            font.setBold(self.bold)
            f = self._cache[px] = (font, QFontMetricsF(font))
        return f


def _draw_text_fit(p: QPainter, fonts: _Fonts, rect: QRectF, text: str, px: float, color: QColor) -> None:
    if not text:
        return
    font, fm = fonts.get(px)
    w = fm.horizontalAdvance(text)
    if w > rect.width() * 0.9 and w > 0:
        font, fm = fonts.get(px * rect.width() * 0.9 / w)
    p.setFont(font)
    p.setPen(color)
    p.drawText(rect, Qt.AlignCenter, text)


def paint_arrow(p: QPainter, box: QRectF, state: "kl.KbmState", style: dict, now: float, fonts=None) -> None:
    fade = style.get("arrow_fade_ms", 0)
    if fade and (now - state.last_move_t) * 1000 > fade:
        return
    curve = kl.arrow_curve(state.burst)
    if curve is None:
        return
    pts = kl.fit_arrow(curve, box.width(), box.height(), style.get("arrow_full_distance", 600))
    pts = [QPointF(box.left() + x, box.top() + y) for x, y in pts]
    if state.burst_src == "m":                  # a macro moved the mouse: output color
        if not style.get("show_macro_output", True):
            return
        color = qcolor(style.get("macro_color", "#5a9ee0ff"))
    else:
        color = qcolor(style.get("arrow_color", "#e0955aff"))
    width = float(style.get("arrow_width", 3.0))
    head = max(width * 3.2, 8.0)
    dx, dy = kl.arrow_head_dir([(q.x(), q.y()) for q in pts])
    tip = pts[3]
    # stop the shaft short of the tip so the round cap doesn't poke through the head
    shaft_end = QPointF(tip.x() - dx * head * 0.7, tip.y() - dy * head * 0.7)
    path = QPainterPath(pts[0])
    path.cubicTo(pts[1], pts[2], shaft_end)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    nx, ny = -dy, dx
    back = QPointF(tip.x() - dx * head, tip.y() - dy * head)
    tri = QPolygonF([tip, QPointF(back.x() + nx * head * 0.55, back.y() + ny * head * 0.55),
                     QPointF(back.x() - nx * head * 0.55, back.y() - ny * head * 0.55)])
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPolygon(tri)
    if style.get("show_move_text"):
        fonts = fonts or _Fonts(style.get("font_family", ""), False)
        font, _fm = fonts.get(max(9, box.height() * 0.12))
        p.setFont(font)
        p.setPen(qcolor(style.get("text_color", "#ffffffff")))
        p.drawText(QRectF(box.left(), box.bottom() - box.height() * 0.16, box.width(), box.height() * 0.16),
                   Qt.AlignCenter, f"{curve['mag']:.0f} px")


def paint_kbm(p: QPainter, layout: dict, style: dict, state: "kl.KbmState", now: float, *, unit=None,
              origin=(0.0, 0.0), names: dict | None = None, highlight: set | None = None) -> None:
    """Draw the picture with its top-left at `origin` (padding included).
    `names` (Dictionary mode): {KEY_name: (big text, small text)} replaces labels.
    `highlight`: names drawn in the pressed color (search matches)."""
    u = float(unit or style["unit"])
    pad = float(style.get("padding", 0))
    ox, oy = origin[0] + pad, origin[1] + pad
    gap = u * style.get("gap", 8) / 100.0
    rad = u * style.get("radius", 14) / 100.0
    key_c = qcolor(style["key_color"])
    edge = qcolor(style["key_outline"])
    ow = float(style.get("outline_width", 1.0))
    pressed_c = qcolor(style["pressed_color"])
    macro_c = qcolor(style["macro_color"])
    text_c = qcolor(style["text_color"])
    ptext_c = qcolor(style["pressed_text_color"])
    fonts = _Fonts(style.get("font_family", ""), bool(style.get("bold")))
    fpx = u * style.get("font_scale", 32) / 100.0
    fade_ms = style.get("release_fade_ms", 0)
    show_timers = style.get("show_timers", True)
    timer_delay = style.get("timer_delay_ms", 0) / 1000.0
    label_mode = style.get("label_mode", "label")
    show_macro = style.get("show_macro_output", True)
    wheel_on = (now - state.wheel_t) * 1000 <= style.get("wheel_flash_ms", 250) and (
        state.wheel_src == "r" or show_macro)
    wheel_c = pressed_c if state.wheel_src == "r" else macro_c
    pad_labels = kl.PAD_LABELS.get(style.get("controller_labels", "xbox"), kl.PAD_LABELS["xbox"])
    highlight = highlight or set()

    bg = qcolor(style.get("background", "#00000000"))
    if bg.alpha():
        p.fillRect(QRectF(origin[0], origin[1], layout["w"] * u + 2 * pad, layout["h"] * u + 2 * pad), bg)

    def fill_for(name):
        """(fill, text color, pressed?)"""
        if name in state.held:
            return pressed_c, ptext_c, True
        if name in highlight:
            return pressed_c, ptext_c, True
        if show_macro and name in state.out_held:
            return macro_c, _contrast(macro_c), True
        rel = state.released.get(name)
        if rel is not None and fade_ms and (now - rel) * 1000 < fade_ms:
            f = (now - rel) * 1000 / fade_ms
            c = _blend(pressed_c, key_c, f)
            return c, _blend(ptext_c, text_c, f), False
        return key_c, text_c, False

    def label_for(item, pressed):
        name = item["name"]
        if show_timers and name in state.held and now - state.held[name] >= timer_delay:
            return kl.format_hold(now - state.held[name])
        if show_timers and show_macro and name in state.out_held and now - state.out_held[name] >= timer_delay:
            return kl.format_hold(now - state.out_held[name])
        if label_mode == "none":
            return ""
        if label_mode == "name" and name:
            return name
        return pad_labels.get(name, item["label"]) if name.startswith("BTN_") else item["label"]

    def src_color(src):
        return pressed_c if src == "r" else macro_c

    def axis(name):
        v, src = state.axis_value(name)
        if src == "m" and not show_macro:
            return 0.0, src
        return v, src

    def shape(r, rr, fill, pressed):
        if pressed and style.get("glow"):
            glow = QColor(fill)
            glow.setAlpha(min(90, fill.alpha()))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            g = max(2.0, u * 0.08)
            p.drawRoundedRect(r.adjusted(-g, -g, g, g), rr + g, rr + g)
        p.setPen(QPen(edge, ow) if ow > 0 else Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(r, rr, rr)

    def controller_item(it, r) -> None:
        kind, name = it["kind"], it["name"]
        if kind == "pad_body":
            body = QColor(key_c).darker(135)
            body.setAlpha(key_c.alpha())
            shape_path = QPainterPath()
            shape_path.addRoundedRect(QRectF(r.left(), r.top(), r.width(), r.height() * 0.7), r.height() * 0.3,
                                      r.height() * 0.3)
            for gx in (r.left() + r.width() * 0.02, r.right() - r.width() * 0.32):   # the two grips
                grip = QPainterPath()
                grip.addEllipse(QRectF(gx, r.top() + r.height() * 0.3, r.width() * 0.3, r.height() * 0.7))
                shape_path = shape_path.united(grip)       # one shape: no darker overlaps when translucent
            p.setPen(QPen(edge, ow) if ow > 0 else Qt.NoPen)
            p.setBrush(body)
            p.drawPath(shape_path)
            return
        fill, tcol, pressed = fill_for(name)
        if kind == "trigger":
            v, src = axis(it["axis"])
            if pressed and v < 1.0 and name in state.held:
                v, src = 1.0, "r"
            rr = r.height() * 0.3
            shape(r, rr, key_c, False)
            if v > 0.001:
                level = QRectF(r.left(), r.bottom() - r.height() * v, r.width(), r.height() * v)
                p.save()
                clip = QPainterPath()
                clip.addRoundedRect(r, rr, rr)
                p.setClipPath(clip)
                p.fillRect(level, src_color(src))
                p.restore()
            full = v > 0.5
            _draw_text_fit(p, fonts, r, label_for(it, pressed) if name in state.held
                           else pad_labels.get(name, ""), fpx, ptext_c if full else text_c)
            return
        if kind == "stick":
            base = QColor(key_c).darker(120)
            base.setAlpha(key_c.alpha())
            p.setPen(QPen(edge, ow) if ow > 0 else Qt.NoPen)
            p.setBrush(base)
            p.drawEllipse(r)
            vx, sx = axis(it["ax"])
            vy, sy = axis(it["ay"])
            k = r.width() * 0.62
            travel = (r.width() - k) / 2
            knob = QRectF(r.center().x() - k / 2 + vx * travel, r.center().y() - k / 2 + vy * travel, k, k)
            moved = abs(vx) > 0.12 or abs(vy) > 0.12
            if pressed:
                kfill = fill
            else:
                kfill = key_c.lighter(118)
            if pressed and style.get("glow"):
                glow = QColor(kfill)
                glow.setAlpha(min(90, kfill.alpha()))
                p.setPen(Qt.NoPen)
                p.setBrush(glow)
                g = max(2.0, u * 0.08)
                p.drawEllipse(knob.adjusted(-g, -g, g, g))
            ring = src_color(sx if abs(vx) >= abs(vy) else sy)
            p.setPen(QPen(ring, max(2.0, u * 0.1)) if moved else (QPen(edge, ow) if ow > 0 else Qt.NoPen))
            p.setBrush(kfill)
            p.drawEllipse(knob)
            _draw_text_fit(p, fonts, knob, label_for(it, pressed), fpx * 0.9, tcol if pressed else text_c)
            return
        if kind == "dpad":
            hat_axis, want = it["hat"]
            hv, hsrc = axis(hat_axis)
            if not pressed and ((want < 0 and hv < -0.5) or (want > 0 and hv > 0.5)):
                fill, tcol, pressed = src_color(hsrc), ptext_c, True
            shape(r, r.width() * 0.18, fill, pressed)
            _draw_text_fit(p, fonts, r, label_for(it, pressed) if name in state.held
                           else pad_labels.get(name, ""), fpx * 0.9, tcol)
            return
        if kind == "pad_btn":
            if pressed and style.get("glow"):
                glow = QColor(fill)
                glow.setAlpha(min(90, fill.alpha()))
                p.setPen(Qt.NoPen)
                p.setBrush(glow)
                g = max(2.0, u * 0.08)
                p.drawEllipse(r.adjusted(-g, -g, g, g))
            p.setPen(QPen(edge, ow) if ow > 0 else Qt.NoPen)
            p.setBrush(fill)
            p.drawEllipse(r)
            _draw_text_fit(p, fonts, r, label_for(it, pressed), fpx, tcol)
            return
        # shoulder / small
        shape(r, min(r.width(), r.height()) * 0.45, fill, pressed)
        _draw_text_fit(p, fonts, r, label_for(it, pressed), fpx * (0.8 if kind == "small" else 1.0), tcol)

    for it in layout["items"]:
        kind = it["kind"]
        r = QRectF(ox + it["x"] * u, oy + it["y"] * u, it["w"] * u, it["h"] * u)
        if kind == "arrow_box":
            if style.get("show_arrow", True):
                paint_arrow(p, r, state, style, now, fonts)
            continue
        if kind == "mouse_body":
            p.setPen(QPen(edge, ow) if ow > 0 else Qt.NoPen)
            p.setBrush(key_c)
            p.drawRoundedRect(r, r.width() * 0.42, r.width() * 0.42)
            continue
        if kind in ("pad_body", "trigger", "stick", "dpad", "pad_btn", "shoulder", "small"):
            controller_item(it, r)
            continue
        if kind == "key":
            r = r.adjusted(0, 0, -gap, -gap)
        name = it["name"]
        fill, tcol, pressed = fill_for(name)
        if kind == "wheel" and wheel_on and state.wheel_dir:
            fill, tcol, pressed = wheel_c, _contrast(wheel_c), True
        if pressed and style.get("glow") and kind != "wheel":
            glow = QColor(fill)
            glow.setAlpha(min(90, fill.alpha()))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            g = max(2.0, u * 0.08)
            p.drawRoundedRect(r.adjusted(-g, -g, g, g), rad + g, rad + g)
        p.setPen(QPen(edge, ow) if ow > 0 else Qt.NoPen)
        p.setBrush(fill if kind == "key" else (fill if pressed else key_c.lighter(112)))
        rr = rad if kind == "key" else min(r.width(), r.height()) * 0.3
        p.drawRoundedRect(r, rr, rr)
        if kind == "wheel":
            if wheel_on and state.wheel_dir:
                _draw_text_fit(p, fonts, r, "▲" if state.wheel_dir > 0 else "▼", r.width() * 0.8, tcol)
            continue
        if names is not None and kind in ("key", "mouse_btn"):
            big, small = names.get(name, (it["label"], name))
            if small and small != big:
                _draw_text_fit(p, fonts, QRectF(r.left(), r.top(), r.width(), r.height() * 0.62), big, fpx, tcol)
                _draw_text_fit(p, fonts, QRectF(r.left(), r.top() + r.height() * 0.55, r.width(), r.height() * 0.4),
                               small, fpx * 0.55, tcol)
            else:
                _draw_text_fit(p, fonts, r, big, fpx, tcol)
            continue
        _draw_text_fit(p, fonts, r, label_for(it, pressed), fpx, tcol)


def picture_size(layout: dict, style: dict, unit=None) -> tuple:
    u = float(unit or style["unit"])
    pad = float(style.get("padding", 0))
    return layout["w"] * u + 2 * pad, layout["h"] * u + 2 * pad


def layout_for(style: dict) -> dict:
    return kl.layout_for_style(style)


def needs_animation(state: "kl.KbmState", style: dict, now: float) -> bool:
    """True while something on the picture changes with time alone
    (timers ticking, fades, the wheel flash, the arrow timing out)."""
    if style.get("show_timers", True) and (state.held or (state.out_held and style.get("show_macro_output", True))):
        return True
    fade = style.get("release_fade_ms", 0)
    if fade and any((now - t) * 1000 < fade for t in state.released.values()):
        return True
    if (now - state.wheel_t) * 1000 <= style.get("wheel_flash_ms", 250) + 50:
        return True
    af = style.get("arrow_fade_ms", 0)
    return bool(af) and (now - state.last_move_t) * 1000 <= af + 50


class KbmView(QWidget):
    """A widget showing the picture, scaled to fit its size."""

    def __init__(self, style: dict, state: "kl.KbmState | None" = None, parent=None, clock=None):
        super().__init__(parent)
        import time as _time
        self.clock = clock or _time.perf_counter
        self.style_ = dict(style)
        self.state = state or kl.KbmState()
        self.layout_ = layout_for(self.style_)
        self.names = None
        self.highlight: set = set()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(120)

    def set_style(self, style: dict) -> None:
        self.style_ = dict(style)
        self.layout_ = layout_for(self.style_)
        self.updateGeometry()
        self.update()

    def heightForWidth(self, w: int) -> int:
        lw, lh = self.layout_["w"], self.layout_["h"]
        pad = 2 * float(self.style_.get("padding", 0))
        return int((w - pad) * lh / max(lw, 1) + pad)

    def hasHeightForWidth(self) -> bool:
        return True

    def unit(self) -> float:
        pad = 2 * float(self.style_.get("padding", 0))
        return max(4.0, min((self.width() - pad) / max(self.layout_["w"], 1),
                            (self.height() - pad) / max(self.layout_["h"], 1)))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        u = self.unit()
        w, h = picture_size(self.layout_, self.style_, u)
        paint_kbm(p, self.layout_, self.style_, self.state, self.clock(), unit=u,
                  origin=((self.width() - w) / 2, (self.height() - h) / 2), names=self.names,
                  highlight=self.highlight)
