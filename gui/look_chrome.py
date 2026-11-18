"""The parts of a look (gui/themes.py) that are structure rather than paint:

LookFrame   -- the main window's central widget. Paints what surrounds the
               pages (a stage with a valance and footlights, a lit screen,
               a wooden bench, a terminal status line, a drawing border, an
               arcade cabinet, felt, a craft table) and reserves room for it.
NavTab      -- one navigation entry, drawn the look's way (a playbill line,
               a puppet on a rod, an index tab, a function key, a drawing
               sheet tab, an arcade label, a sewn tab, a taped paper tab).
build_nav() -- the navigation strip for a look: the tabs in a row or a
               column, on the side the look puts them.

Classic (nav_style "segment", frame "plain") keeps the kit's own sidebar
buttons and draws no frame.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QVBoxLayout, QWidget

from ui_kit.theme import Theme

NAV_SHORT = {"Macros": "Macros", "Macro Editor": "Editor", "Input Visualizer": "Visualizer",
             "Dictionary": "Dictionary", "Settings": "Settings"}

# the paper colors of tab styles that color each tab differently
TAPED_COLORS = ("#f2c14e", "#e8823a", "#7ec4e8", "#c39ae0", "#9fd47a")
INDEX_COLORS = ("#c46a1b", "#6e675b", "#2f6fb0", "#7b5ea7", "#4f6d7a")


def _c(hex_: str, alpha: int = 255) -> QColor:
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _ink_on(c: QColor) -> QColor:
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return QColor("#1a1410") if lum > 0.55 else QColor("#fbf6ea")


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------

class NavTab(QAbstractButton):
    """A checkable navigation entry painted in the look's nav_style."""

    def __init__(self, text: str, index: int, style: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.style_name = style
        self.index = index            # position in the strip (tab colors, F-key numbers, tilt)
        self.setAccessibleName(text)

    # -- sizing ------------------------------------------------------------------
    def _label(self) -> str:
        t = self.text()
        if self.style_name == "fkeys":
            return NAV_SHORT.get(t, t).lower()
        if self.style_name in ("sheets", "arcade"):
            return NAV_SHORT.get(t, t).upper()
        if self.style_name in ("rod", "taped", "sewn", "index"):
            return NAV_SHORT.get(t, t)
        return t

    def _font(self) -> QFont:
        s = Theme().settings
        f = QFont(self.font())
        family = getattr(s, "font_display", "") or getattr(s, "font_body", "")
        if self.style_name in ("playbill", "rod", "taped", "sewn") and family:
            f.setFamily(family)
        if self.style_name == "playbill":
            f.setPixelSize(19)
            f.setBold(True)
        elif self.style_name == "rod":
            f.setPixelSize(14)
            f.setBold(True)
        elif self.style_name == "arcade":
            f.setFamily("Press Start 2P")
            f.setPixelSize(10)
        elif self.style_name in ("taped", "sewn"):
            f.setPixelSize(15 if self.style_name == "taped" else 17)
            f.setBold(self.style_name == "taped")
        elif self.style_name in ("index", "sheets"):
            f.setBold(True)
            f.setLetterSpacing(QFont.PercentageSpacing, 108)
        return f

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self._font())
        tw = fm.horizontalAdvance(self._label())
        st = self.style_name
        if st == "playbill":
            return QSize(180, 50)   # fits the 220px playbill card inside its margins
        if st == "index":
            return QSize(46, int(tw) + 34)
        if st == "fkeys":
            return QSize(int(tw + fm.horizontalAdvance("F10 ") + 22), 28)
        if st == "rod":
            return QSize(int(tw) + 44, 74)
        if st == "sheets":
            return QSize(int(fm.horizontalAdvance(f"SHT {self.index + 1} \u00b7 {self._label()}")) + 36, 38)
        if st == "arcade":
            return QSize(int(tw) + 30, 34)
        if st == "sewn":
            return QSize(int(tw) + 48, 52)
        if st == "taped":
            return QSize(int(tw) + 44, 50)
        return QSize(int(tw) + 30, 36)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # -- painting ------------------------------------------------------------------
    def paintEvent(self, _e) -> None:
        theme = Theme()
        s = theme.settings
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(self._font())
        on = self.isChecked()
        hover = self.underMouse()
        r = QRectF(self.rect())
        text = QColor(s.color_text)
        label = self._label()
        getattr(self, "_paint_" + self.style_name, self._paint_plain)(p, r, on, hover, text, label, s)
        if self.hasFocus():
            pen = QPen(_c(s.color_accent), 1, Qt.DotLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(r.adjusted(1, 1, -2, -2))
        p.end()

    def _paint_plain(self, p, r, on, hover, text, label, s):
        p.setPen(text)
        p.drawText(r, Qt.AlignCenter, label)

    def _paint_playbill(self, p, r, on, hover, text, label, s):
        # a line in a theatre programme: velvet when current
        inner = r.adjusted(4, 3, -4, -3)
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(_c(s.color_general).darker(170))
            p.drawRoundedRect(inner, 3, 3)
            p.setPen(_c(s.color_input))
        else:
            p.setPen(_c("#2a1719") if not hover else _c(s.color_general).darker(130))
        p.drawText(inner.adjusted(12, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, label)

    def _paint_rod(self, p, r, on, hover, text, label, s):
        # a silhouette puppet on a rod: the current one is raised and outlined
        lift = 14 if on else (6 if hover else 0)
        body_h = 36 + (4 if on else 0)
        top = r.bottom() - 30 - body_h - lift + 10
        body = QRectF(r.left() + 4, top, r.width() - 8, body_h)
        path = QPainterPath()
        path.moveTo(body.left() + 8, body.top())
        path.lineTo(body.right() - 8, body.top())
        path.lineTo(body.right(), body.bottom())
        path.lineTo(body.left(), body.bottom())
        path.closeSubpath()
        p.setPen(QPen(_c(s.color_input), 3) if on else Qt.NoPen)
        p.setBrush(_c(s.color_text))
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.drawRect(QRectF(r.center().x() - 1.5, body.bottom(), 3, r.bottom() - body.bottom()))
        p.setPen(_c(s.color_page_background))
        p.drawText(body, Qt.AlignCenter, label)

    def _paint_index(self, p, r, on, hover, text, label, s):
        # a colored index tab on the right edge: the current one is paper-colored and sticks out further
        fill = _c(s.color_page_background) if on else _c(INDEX_COLORS[self.index % len(INDEX_COLORS)])
        if hover and not on:
            fill = fill.lighter(112)
        out = 0 if on else 8
        body = QRectF(r.left(), r.top() + 2, r.width() - out, r.height() - 4)
        path = QPainterPath()
        rad = 7
        path.moveTo(body.left(), body.top())
        path.lineTo(body.right() - rad, body.top())
        path.quadTo(body.right(), body.top(), body.right(), body.top() + rad)
        path.lineTo(body.right(), body.bottom() - rad)
        path.quadTo(body.right(), body.bottom(), body.right() - rad, body.bottom())
        path.lineTo(body.left(), body.bottom())
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPath(path)
        p.save()
        p.translate(body.center())
        p.rotate(90)
        p.setPen(_c(s.color_text) if on else _ink_on(fill))
        p.drawText(QRectF(-body.height() / 2, -body.width() / 2, body.height(), body.width()), Qt.AlignCenter, label)
        p.restore()

    def _paint_fkeys(self, p, r, on, hover, text, label, s):
        fm = QFontMetricsF(p.font())
        key = f"F{[1, 2, 5, 6, 9][self.index % 5]}"
        kw = fm.horizontalAdvance(key) + 8
        key_r = QRectF(r.left() + 2, r.center().y() - fm.height() / 2 - 1, kw, fm.height() + 2)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(s.color_input))
        p.drawRect(key_r)
        p.setPen(_c(s.color_app_background))
        p.drawText(key_r, Qt.AlignCenter, key)
        p.setPen(_c(s.color_input) if on else (text if hover else _c(s.color_text, 190)))
        f = QFont(p.font())
        f.setUnderline(on)
        p.setFont(f)
        p.drawText(QRectF(key_r.right() + 6, r.top(), r.width() - kw - 8, r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, label)

    def _paint_sheets(self, p, r, on, hover, text, label, s):
        # a sheet tab of a drawing set: inverted when current
        line = _c(s.color_accent)
        p.setPen(QPen(line, 1))
        p.setBrush(line if on else (_c(s.color_highlight) if hover else Qt.NoBrush))
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        p.setPen(_c(s.color_app_background) if on else text)
        p.drawText(r, Qt.AlignCenter, f"SHT {self.index + 1} · {label}")

    def _paint_arcade(self, p, r, on, hover, text, label, s):
        p.setPen(_c(s.color_accent) if on else (text if hover else _c(s.color_neutral_block)))
        p.drawText(r, Qt.AlignCenter, ("▶ " + label) if on else label)

    def _paint_sewn(self, p, r, on, hover, text, label, s):
        # a felt tab sewn onto the bottom edge: rounded top, stitched border
        lift = 0 if on else 6
        body = QRectF(r.left() + 2, r.top() + lift, r.width() - 4, r.height() - lift)
        fill = _c(s.color_general) if on else (_c(s.color_highlight).lighter(110) if hover else _c(s.color_highlight))
        path = QPainterPath()
        rad = 20
        path.moveTo(body.left(), body.bottom())
        path.lineTo(body.left(), body.top() + rad)
        path.quadTo(body.left(), body.top(), body.left() + rad, body.top())
        path.lineTo(body.right() - rad, body.top())
        path.quadTo(body.right(), body.top(), body.right(), body.top() + rad)
        path.lineTo(body.right(), body.bottom())
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPath(path)
        stitch = QPen(_c("#ffffff", 170), 1.6, Qt.DashLine)
        p.setPen(stitch)
        p.setBrush(Qt.NoBrush)
        inner = QPainterPath()
        b2 = body.adjusted(5, 5, -5, 0)
        rad2 = rad - 5
        inner.moveTo(b2.left(), b2.bottom())
        inner.lineTo(b2.left(), b2.top() + rad2)
        inner.quadTo(b2.left(), b2.top(), b2.left() + rad2, b2.top())
        inner.lineTo(b2.right() - rad2, b2.top())
        inner.quadTo(b2.right(), b2.top(), b2.right(), b2.top() + rad2)
        inner.lineTo(b2.right(), b2.bottom())
        p.drawPath(inner)
        p.setPen(_ink_on(fill))
        p.drawText(body.adjusted(0, 4, 0, 0), Qt.AlignCenter, label)

    def _paint_taped(self, p, r, on, hover, text, label, s):
        # a colored paper tab taped to the top of the book, each at its own small tilt
        tilt = (-2.0, 1.2, 2.0, -1.0, 2.5)[self.index % 5]
        fill = _c(s.color_page_background) if on else _c(TAPED_COLORS[self.index % len(TAPED_COLORS)])
        if hover and not on:
            fill = fill.lighter(108)
        drop = 0 if on else 6
        p.save()
        p.translate(r.center())
        p.rotate(tilt)
        body = QRectF(-r.width() / 2 + 3, -r.height() / 2 + 6 + drop, r.width() - 6, r.height() - 6 - drop + 8)
        p.setPen(Qt.NoPen)
        p.setBrush(_c("#000000", 50))
        p.drawRoundedRect(body.translated(0, 2), 9, 9)
        p.setBrush(fill)
        p.drawRoundedRect(body, 9, 9)
        if on:
            p.setBrush(_c(s.color_general))
            p.drawRect(QRectF(body.left() + 10, body.bottom() - 11, body.width() - 20, 3))
        # the tape
        p.setBrush(_c("#fffae1", 190))
        p.drawRect(QRectF(-17, body.top() - 6, 34, 12))
        p.setPen(_c(s.color_text) if on else _ink_on(fill))
        p.drawText(body.adjusted(0, 0, 0, -6), Qt.AlignCenter, label)
        p.restore()


class NavStrip(QWidget):
    """Holds the tabs; paints what sits behind them (the playbill card,
    the rod rail, the F-key bar)."""

    def __init__(self, style: str, vertical: bool, parent=None):
        super().__init__(parent)
        self.style_name = style
        self.vertical = vertical
        box = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        box.setSpacing({"playbill": 2, "index": 4, "fkeys": 14, "sheets": 0, "arcade": 18,
                        "rod": 28, "sewn": 10, "taped": 8}.get(style, 6))
        m = {"playbill": (18, 44, 18, 16), "index": (0, 26, 0, 0), "fkeys": (12, 4, 12, 4),
             "sheets": (0, 0, 0, 0), "arcade": (6, 2, 6, 2), "rod": (0, 0, 0, 0), "sewn": (0, 0, 0, 0),
             "taped": (60, 0, 0, 0)}.get(style, (0, 0, 0, 0))
        box.setContentsMargins(*m)
        if style in ("rod", "sewn"):
            box.addStretch(1)            # centered row along the bottom
        self.box = box

    def finish(self) -> None:
        self.box.addStretch(1)
        if self.style_name == "playbill":
            self.setFixedWidth(220)

    def paintEvent(self, _e) -> None:
        s = Theme().settings
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        if self.style_name == "playbill":
            card = r.adjusted(6, 6, -6, -6)
            p.setPen(QPen(_c(s.color_accent), 1))
            p.setBrush(_c("#efe2c8"))
            p.drawRoundedRect(card, 4, 4)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(card.adjusted(4, 4, -4, -4), 2, 2)
            f = QFont(self.font())
            fam = getattr(s, "font_display", "")
            if fam:
                f.setFamily(fam)
            f.setPixelSize(12)
            f.setLetterSpacing(QFont.PercentageSpacing, 140)
            f.setCapitalization(QFont.AllUppercase)
            p.setFont(f)
            p.setPen(_c("#2a1719"))
            p.drawText(QRectF(card.left(), card.top() + 10, card.width(), 18), Qt.AlignCenter, "Tonight's programme")
            p.setPen(QPen(_c("#8c6b3e"), 1))
            p.drawLine(QPointF(card.left() + 16, card.top() + 34), QPointF(card.right() - 16, card.top() + 34))
        elif self.style_name == "fkeys":
            p.fillRect(r, _c(s.color_app_background))
        elif self.style_name == "sheets":
            p.setPen(QPen(_c(s.color_accent), 1))
            p.drawLine(r.bottomLeft(), r.bottomRight())
        p.end()


def build_nav(settings, labels: list, make_segment) -> tuple:
    """The navigation strip for a look. `labels` = [(page index, text)];
    `make_segment(idx, text)` builds a classic sidebar button. Returns
    (strip widget, {page index: button})."""
    style = getattr(settings, "nav_style", "segment") or "segment"
    position = getattr(settings, "nav_position", "left") or "left"
    vertical = position in ("left", "right")
    buttons = {}
    if style == "segment":
        strip = QWidget()
        strip.setFixedWidth(170)
        box = QVBoxLayout(strip)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(Theme().padding)
        for idx, text in labels:
            b = make_segment(idx, text)
            buttons[idx] = b
            box.addWidget(b)
        box.addStretch(1)
        return strip, buttons
    strip = NavStrip(style, vertical)
    for i, (idx, text) in enumerate(labels):
        b = NavTab(text, i, style)
        buttons[idx] = b
        align = 0
        if style == "playbill":
            align = 0
        elif not vertical:
            align = Qt.AlignBottom if position == "top" else Qt.AlignTop
        strip.box.addWidget(b, 0, align) if align else strip.box.addWidget(b)
    strip.finish()
    return strip, buttons


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------

FRAME_MARGINS = {   # extra room around the content, beyond the look's padding (left, top, right, bottom)
    "stage": (0, 30, 0, 22), "lightbox": (8, 8, 8, 4), "desk": (6, 6, 6, 6), "terminal": (0, 24, 0, 0),
    "drawing": (10, 10, 10, 10), "cabinet": (0, 0, 0, 0), "felt": (0, 0, 0, 0), "table": (8, 4, 8, 8),
}


class LookFrame(QWidget):
    """The main window's central widget: paints the look's frame behind and
    around everything, and pads the layout so the frame shows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status_text = lambda: ""        # Console's status line (set by the window)

    def frame_kind(self) -> str:
        return getattr(Theme().settings, "frame", "plain") or "plain"

    def margins(self, pad: int) -> tuple:
        ex = FRAME_MARGINS.get(self.frame_kind(), (0, 0, 0, 0))
        return tuple(pad + e for e in ex)

    def paintEvent(self, e) -> None:
        # The frame only changes with the window size or the look, so it is
        # drawn once into a pixmap and copied from there on every repaint
        # (repaints happen whenever anything over it changes: hovers, tabs).
        s = Theme().settings
        kind = self.frame_kind()
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, kind, s.color_app_background, s.color_page_background,
               s.color_accent, s.color_text, s.color_input, s.color_general, s.color_highlight)
        cached = getattr(self, "_frame_cache", None)
        if cached is None or cached[0] != key:
            from PySide6.QtGui import QPixmap
            pm = QPixmap(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))
            pm.setDevicePixelRatio(dpr)
            bg = _c(s.color_app_background)
            pm.fill(bg)
            cp = QPainter(pm)
            cp.setRenderHint(QPainter.Antialiasing)
            getattr(self, "_frame_" + kind, lambda *a: None)(cp, QRectF(0, 0, self.width(), self.height()), s, bg)
            cp.end()
            self._frame_cache = cached = (key, pm)
        p = QPainter(self)
        p.drawPixmap(e.rect(), cached[1], QRectF(e.rect().x() * dpr, e.rect().y() * dpr,
                                                e.rect().width() * dpr, e.rect().height() * dpr).toRect())
        if kind == "terminal":                 # the status line's text is live (clock, profile)
            self._terminal_text(p, QRectF(self.rect()), s)
        p.end()

    def _frame_stage(self, p, r, s, bg):
        # a velvet valance with a scalloped hem, a brass rail, footlight glow along the bottom
        velvet = _c(s.color_general).darker(160)
        p.setPen(Qt.NoPen)
        p.setBrush(velvet)
        p.drawRect(QRectF(r.left(), r.top(), r.width(), 22))
        step = 40
        x = r.left()
        while x < r.right():
            p.drawEllipse(QPointF(x + step / 2, r.top() + 22), step / 2, 10)
            x += step
        p.setBrush(_c(s.color_accent))
        p.drawRect(QRectF(r.left(), r.top() + 20, r.width(), 3))
        glow_y = r.bottom() - 4
        x = r.left() + 40
        while x < r.right():
            g = QRadialGradient(QPointF(x, glow_y), 46)
            g.setColorAt(0.0, _c(s.color_input, 90))
            g.setColorAt(1.0, _c(s.color_input, 0))
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(x, glow_y), 46, 22)
            x += 80

    def _frame_lightbox(self, p, r, s, bg):
        g = QRadialGradient(r.center() - QPointF(0, r.height() * 0.08), max(r.width(), r.height()) * 0.62)
        glow = _c(s.color_page_background)
        g.setColorAt(0.0, glow.lighter(108) if glow.lightness() > 128 else glow.lighter(160))
        g.setColorAt(0.5, _c(s.color_page_background))
        g.setColorAt(1.0, bg)
        p.fillRect(r, QBrush(g))
        p.setPen(QPen(_c(s.color_text), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(7, 7, -7, -7))

    def _frame_desk(self, p, r, s, bg):
        p.fillRect(r, QBrush(_stripe_tile(38, 2, _c("#000000", 26))))

    def _frame_terminal(self, p, r, s, bg):
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 22), _c(s.color_input))

    def _terminal_text(self, p, r, s):
        bg = _c(s.color_app_background)
        bar = QRectF(r.left(), r.top(), r.width(), 22)
        f = QFont(self.font())
        if getattr(s, "font_mono", ""):
            f.setFamily(s.font_mono)
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(bg)
        p.drawText(bar.adjusted(12, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, "puppetry   " + (self.status_text() or ""))
        p.drawText(bar.adjusted(12, 0, -12, 0), Qt.AlignVCenter | Qt.AlignRight, time.strftime("%H:%M"))

    def _frame_drawing(self, p, r, s, bg):
        line = _c(s.color_accent)
        minor, major = _mix(bg, line, 0.06), _mix(bg, line, 0.12)
        p.setPen(QPen(minor, 1))
        for i, x in enumerate(range(int(r.left()), int(r.right()), 24)):
            p.setPen(QPen(major if i % 5 == 0 else minor, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for i, y in enumerate(range(int(r.top()), int(r.bottom()), 24)):
            p.setPen(QPen(major if i % 5 == 0 else minor, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(line, 2))
        p.drawRect(r.adjusted(6, 6, -6, -6))
        p.setPen(QPen(_c(s.color_accent, 150), 1))
        p.drawRect(r.adjusted(11, 11, -11, -11))

    def _frame_cabinet(self, p, r, s, bg):
        p.fillRect(r, QBrush(_scan_tile(_c("#ffffff", 9))))

    def _frame_felt(self, p, r, s, bg):
        p.fillRect(r, QBrush(_dot_tile(6, _c("#ffffff", 10))))

    def _frame_table(self, p, r, s, bg):
        p.fillRect(r, QBrush(_grain_tile()))
        g = QRadialGradient(r.center() - QPointF(0, r.height() * 0.1), max(r.width(), r.height()) * 0.55)
        g.setColorAt(0.0, _c("#ffd696", 70))
        g.setColorAt(1.0, _c("#ffd696", 0))
        p.fillRect(r, QBrush(g))


# ---- texture tiles (each built once) -------------------------------------------

_tiles: dict = {}


def _tile(key, w: int, h: int, draw):
    t = _tiles.get(key)
    if t is None:
        from PySide6.QtGui import QPixmap
        t = QPixmap(w, h)
        t.fill(Qt.transparent)
        p = QPainter(t)
        draw(p)
        p.end()
        _tiles[key] = t
    return t


def _stripe_tile(step: int, width: int, color: QColor):
    return _tile(("stripe", step, width, color.rgba()), step, 8,
                 lambda p: p.fillRect(QRectF(0, 0, width, 8), color))


def _scan_tile(color: QColor):
    return _tile(("scan", color.rgba()), 8, 4, lambda p: p.fillRect(QRectF(0, 0, 8, 2), color))


def _dot_tile(step: int, color: QColor):
    def draw(p):
        p.setPen(QPen(color, 1.5))
        p.drawPoint(QPointF(3, 3))
    return _tile(("dot", step, color.rgba()), step, step, draw)


def _grain_tile():
    def draw(p):
        for i in range(3):
            x = i * 29
            p.fillRect(QRectF(x, 0, 3, 8), _c("#000000", 40 if i == 0 else 26))
            p.fillRect(QRectF(x + 11, 0, 2, 8), _c("#ffffff", 10))
    return _tile(("grain",), 87, 8, draw)
