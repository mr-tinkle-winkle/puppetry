"""
Puppetry-specific widgets, painted the same way as the ui_kit ones
(UI_THEMING_GUIDE.md checklist step 8): colors from Theme, shapes from
rounded_rect_path, spacing from Theme().padding, PressPulse on anything
clickable.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize, QVariantAnimation, QEasingCurve, Signal, QPointF
from PySide6.QtGui import QPainter, QColor, QFontMetrics, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QDialog, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget,
)

from ui_kit.collapse_toggle_button import CollapseToggleButton
from ui_kit.custom_button import CustomButton
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.page_outline import paint_page_outline
from ui_kit.press_pulse import PressPulse
from ui_kit.rounded_rect import rounded_rect_path
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme, contrast_text


def label_style(color: QColor, extra: str = "") -> str:
    """Always a SCOPED stylesheet (pitfall #2: an unscoped one cascades
    into every descendant and breaks custom painting)."""
    return f"QLabel {{ color: {color.name()}; {extra} }}"


# ---------------------------------------------------------------------------
# ToggleSwitch -- the per-profile "enabled" switch on each macro row
# ---------------------------------------------------------------------------

class ToggleSwitch(QAbstractButton):
    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._pulse = PressPulse(self)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(46, 26)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, v) -> None:
        self._pos = float(v)
        self.update()

    def paintEvent(self, _event) -> None:
        theme = Theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._pulse.apply(p)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        track_off, track_on = theme.surface(), theme.accent()
        t = self._pos
        track = QColor(
            round(track_off.red() + (track_on.red() - track_off.red()) * t),
            round(track_off.green() + (track_on.green() - track_off.green()) * t),
            round(track_off.blue() + (track_on.blue() - track_off.blue()) * t),
        )
        if not self.isEnabled():
            track = track.darker(140)
        p.fillPath(rounded_rect_path(r, r.height() / 2), track)
        # outline so the OFF state (surface on page background) stays visible
        p.setPen(QPen(theme.accent(), 1.2))
        p.drawPath(rounded_rect_path(r, r.height() / 2))
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * t
        p.setPen(Qt.NoPen)
        p.setBrush(contrast_text(track))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


# ---------------------------------------------------------------------------
# NameBox -- macro name in its own rounded box; click to rename inline
# ---------------------------------------------------------------------------

class _NameLabel(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pulse = PressPulse(self)
        self.text = ""
        self.setCursor(Qt.IBeamCursor)

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(max(120, fm.horizontalAdvance(self.text) + 40), max(30, fm.height() + 12))  # 14px pad each side + border + pulse

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()) and self.isEnabled():
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        theme = Theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._pulse.apply(p)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        fill = theme.surface()
        if self.underMouse() and self.isEnabled():
            fill = fill.lighter(115)
        p.fillPath(rounded_rect_path(r, theme.corner_radius(10)), fill)
        p.setPen(contrast_text(fill) if self.isEnabled() else contrast_text(fill).darker(150))
        p.drawText(r.adjusted(14, 0, -14, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text)


class NameBox(QStackedWidget):
    renamed = Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._label = _NameLabel()
        self._label.text = text
        self._label.setToolTip("Click to rename")
        self._edit = CustomLineEdit(text)
        self.addWidget(self._label)
        self.addWidget(self._edit)
        self._label.clicked.connect(self._begin)
        self._edit.returnPressed.connect(self._commit)
        self._edit.editingFinished.connect(self._commit)
        self._editing = False

    def text(self) -> str:
        return self._label.text

    def sizeHint(self) -> QSize:
        return self._label.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return QSize(120, self._label.sizeHint().height())

    def _begin(self) -> None:
        self._editing = True
        self._edit.setText(self._label.text)
        self.setCurrentWidget(self._edit)
        self._edit.setFocus()
        self._edit.selectAll()

    def _commit(self) -> None:
        if not self._editing:
            return
        self._editing = False
        new = self._edit.text().strip()
        self.setCurrentWidget(self._label)
        if new and new != self._label.text:
            self._label.text = new
            self._label.updateGeometry()
            self._label.update()
            self.renamed.emit(new)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and self._editing:
            self._editing = False
            self.setCurrentWidget(self._label)
            return
        super().keyPressEvent(event)


class LockToggle(CustomButton):
    """🔒/🔓 -- locking blocks renaming, rebinding, trigger-edge changes,
    Edit and Delete for that macro until unlocked (e.g. for a macro with
    sensitive code you don't want casually reopened while screen-sharing)."""

    def __init__(self, locked: bool = False, parent=None):
        super().__init__("", parent)
        self.setCheckable(True)
        self.setChecked(locked)
        self.set_circular(30)
        self.setToolTip("Lock this macro (blocks rename, combo changes, Edit and Delete until unlocked)")
        self._sync()
        self.toggled.connect(self._sync)

    def _sync(self, *_a) -> None:
        self.setText("\U0001F512" if self.isChecked() else "\U0001F513")


# ---------------------------------------------------------------------------
# Themed dialogs (ui_kit only ships an OK-only message box)
# ---------------------------------------------------------------------------

class ThemedDialog(QDialog):
    def __init__(self, title: str, text: str, buttons: list[str], default: int = 0,
                 edit_text: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._theme = Theme()
        self.choice = -1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        t = QLabel(title)
        t.setStyleSheet(label_style(self._theme.text(), "font-size: 15px; font-weight: bold;"))
        lay.addWidget(t)
        if text:
            body = QLabel(text)
            body.setWordWrap(True)
            body.setStyleSheet(label_style(self._theme.text()))
            lay.addWidget(body)
        self.edit = None
        if edit_text is not None:
            self.edit = CustomLineEdit(edit_text)
            self.edit.selectAll()
            self.edit.returnPressed.connect(lambda: self._pick(default))
            lay.addWidget(self.edit)
        row = QHBoxLayout()
        row.addStretch(1)
        for i, label in enumerate(buttons):
            b = CustomButton(label)
            b.clicked.connect(lambda _=False, i=i: self._pick(i))
            row.addWidget(b)
        lay.addLayout(row)
        self.setMinimumWidth(360)

    def _pick(self, i: int) -> None:
        self.choice = i
        self.accept()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillPath(rounded_rect_path(QRectF(self.rect()), self._theme.corner_radius(16)), self._theme.page_background())


def ask(parent, title: str, text: str, buttons: list[str], default: int = 0) -> int:
    """Returns the clicked button's index, or -1 if dismissed."""
    d = ThemedDialog(title, text, buttons, default, parent=parent)
    d.exec()
    return d.choice


def prompt_text(parent, title: str, initial: str) -> str | None:
    d = ThemedDialog(title, "", ["Cancel", "OK"], default=1, edit_text=initial, parent=parent)
    d.exec()
    return d.edit.text() if d.choice == 1 else None


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

class Collapsible(QWidget):
    """A ">" header that expands/collapses its content."""

    def __init__(self, title: str, content: QWidget, expanded: bool = False, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.toggle = CollapseToggleButton(expanded)
        head.addWidget(self.toggle)
        lbl = QLabel(title)
        lbl.setStyleSheet(label_style(Theme().text(), "font-weight: bold;"))
        head.addWidget(lbl)
        head.addStretch(1)
        lay.addLayout(head)
        self.content = content
        lay.addWidget(content)
        content.setVisible(expanded)
        self.toggle.toggled.connect(content.setVisible)


def section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(label_style(Theme().text(), "font-size: 15px; font-weight: bold;"))
    return lbl


def dim_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(label_style(Theme().text().darker(140)))
    return lbl


class PageBase(QWidget):
    """A top-level page: own background, SmoothScrollArea (every page
    scrolls -- pitfall #4), 3px outline, and autoFillBackground turned
    back off after setWidget() (pitfall #5)."""

    def __init__(self, parent=None, scroll: bool = True):
        super().__init__(parent)
        theme = Theme()
        self._bg = theme.page_background()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 3)
        self.outer_layout = outer  # exposed so a subclass can add a fixed (non-scrolling) bar below the content, e.g. a bottom-right action button
        self.content = QWidget()
        self.content.setAutoFillBackground(False)
        self.content_layout = QVBoxLayout(self.content)
        pad = theme.padding
        self.content_layout.setContentsMargins(pad, pad, pad, pad)
        self.content_layout.setSpacing(pad)
        if scroll:
            self.scroll = SmoothScrollArea(self)
            self.scroll.setWidgetResizable(True)
            self.scroll.setWidget(self.content)
            self.scroll.setAutoFillBackground(False)
            self.scroll.viewport().setAutoFillBackground(False)
            self.content.setAutoFillBackground(False)
            outer.addWidget(self.scroll)
        else:
            outer.addWidget(self.content)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        p.end()
        paint_page_outline(self, self._bg)


# ---------------------------------------------------------------------------
# The input/output color scheme, app-wide: orange = reads the real world
# (recording, key pickers, the mouse readout, the input visualizer),
# blue = acts on it (playback, sounds, the virtual devices).
# ---------------------------------------------------------------------------

def mark_input(widget) -> None:
    _mark(widget, Theme().input_color())


def mark_output(widget) -> None:
    _mark(widget, Theme().output_color())


def _mark(widget, color: QColor) -> None:
    if hasattr(widget, "set_fill_color"):
        widget.set_fill_color(color)
    elif isinstance(widget, QLabel):
        sheet = widget.styleSheet()
        extra = ""
        if "font-weight: bold" in sheet:
            extra = "font-size: 15px; font-weight: bold;"
        widget.setStyleSheet(label_style(color, extra))
