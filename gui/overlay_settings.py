"""
The "OBS & replay overlay" section of the Input Visualizer page, plus the
Customize dialogs (forms generated from overlay_config's schemas, with a
live preview drawn by the same painter the renderer uses).

Changes are written to overlay.json right away; the helper re-reads it, so
style edits show up in OBS live. Turning pages/the replay buffer on or off
(or changing a port) also restarts the daemon after a second of quiet, so
it can start or stop the helper.
"""
from __future__ import annotations

import json
import threading
import time

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QScrollArea,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

import kbm_layout as kl
import overlay_config as oc
import puppetry_config as cfg_mod
from kbm_paint import KbmView, paint_arrow, qcolor
from ui_kit import theme_config
from ui_kit.custom_button import CustomButton
from ui_kit.custom_checkbox import CustomCheckBox
from ui_kit.custom_combo_style import combo_box_stylesheet
from ui_kit.custom_line_edit import CustomLineEdit
from ui_kit.custom_spinbox import CustomDoubleSpinBox, CustomSpinBox
from ui_kit.rounded_rect import rounded_rect_path
from ui_kit.smooth_scroll_area import SmoothScrollArea
from ui_kit.theme import Theme, contrast_text
from widgets import ToggleSwitch, dim_label, label_style, mark_output, section_title


# ---------------------------------------------------------------------------
# Demo input for previews
# ---------------------------------------------------------------------------
def demo_state(clock) -> kl.KbmState:
    now = clock()
    s = kl.KbmState()
    s.key("KEY_W", True, now - 1.234)
    s.key("KEY_LEFTSHIFT", True, now - 0.41)
    s.key("BTN_LEFT", True, now - 0.087)
    t = now - 0.6
    for i in range(20):
        s.move(-12, 0, t + i * 0.01)
    for i in range(10):
        s.move(0, -9, t + 0.2 + i * 0.01)
    s.last_move_t = now
    s.key("KEY_E", True, now - 0.3, "m")          # pressed by a macro: output color
    s.key("BTN_SOUTH", True, now - 0.52)
    s.axis("LX", 0.7, now)
    s.axis("LY", -0.4, now)
    s.axis("RT", 0.6, now, "m")                   # a macro squeezing the right trigger
    return s


class _ColorButton(CustomButton):
    changed = Signal(str)

    def __init__(self, value: str):
        super().__init__("")
        self.value = value
        self.setFixedWidth(90)
        self.clicked.connect(self._pick)
        self._sync()

    def _sync(self) -> None:
        c = qcolor(self.value)
        self.setText(self.value[:7] + (f" {round(c.alpha() / 2.55)}%" if c.alpha() < 255 else ""))
        shown = QColor(c)
        shown.setAlpha(255)
        self.set_fill_color(shown)

    def _pick(self) -> None:
        c = QColorDialog.getColor(qcolor(self.value), self, "Color", QColorDialog.ShowAlphaChannel)
        if c.isValid():
            self.value = "#{:02x}{:02x}{:02x}{:02x}".format(c.red(), c.green(), c.blue(), c.alpha())
            self._sync()
            self.changed.emit(self.value)


class _FontDelegate(QStyledItemDelegate):
    """Font picker rows: the name drawn big in its own font, its category
    small and dim on the right (never truncated -- the popup is sized to fit),
    and a thin rule for separators."""
    NAME_PX = 19

    def _is_sep(self, index) -> bool:
        return index.data(Qt.AccessibleDescriptionRole) == "separator"

    def _name_font(self, index) -> QFont:
        f = QFont(index.data(Qt.UserRole + 1) or "sans-serif")
        f.setPixelSize(self.NAME_PX)
        return f

    def sizeHint(self, option, index) -> QSize:
        if self._is_sep(index):
            return QSize(10, 9)
        fm = QFontMetrics(self._name_font(index))
        cat = QFontMetrics(QApplication.font()).horizontalAdvance(index.data(Qt.UserRole + 2) or "")
        return QSize(fm.horizontalAdvance(index.data(Qt.DisplayRole) or "") + cat + 48, max(30, fm.height() + 10))

    def paint(self, p, option, index) -> None:
        theme = Theme()
        r = option.rect
        p.save()
        if self._is_sep(index):
            c = QColor(theme.text())
            c.setAlpha(60)
            p.setPen(c)
            p.drawLine(r.left() + 8, r.center().y(), r.right() - 8, r.center().y())
            p.restore()
            return
        if option.state & (QStyle.State_Selected | QStyle.State_MouseOver):
            p.fillRect(r, theme.accent())
        text_c = contrast_text(theme.accent()) if option.state & QStyle.State_Selected else theme.text()
        p.setPen(text_c)
        p.setFont(self._name_font(index))
        p.drawText(r.adjusted(10, 0, -10, 0), Qt.AlignVCenter | Qt.AlignLeft, index.data(Qt.DisplayRole) or "")
        dim = QColor(text_c)
        dim.setAlpha(140)
        p.setPen(dim)
        p.setFont(QApplication.font())             # the UI font, not the picked one
        p.drawText(r.adjusted(10, 0, -10, 0), Qt.AlignVCenter | Qt.AlignRight, index.data(Qt.UserRole + 2) or "")
        p.restore()


class FontPicker(QComboBox):
    """The bundled fonts by category (font_catalog.CATALOG), each shown in
    its own font, plus "Other installed font..." for anything on the system.
    on_change(family) fires when the choice changes."""
    OTHER = "__other__"

    def __init__(self, value: str, on_change, parent=None):
        super().__init__(parent)
        import font_catalog
        font_catalog.register_qt_fonts()
        self._on_change = on_change
        self.setStyleSheet(combo_box_stylesheet(theme_config.get_settings()))
        self.setItemDelegate(_FontDelegate(self))
        self.setMaxVisibleItems(18)
        last_cat = None
        for family, cat, _stem, _w in font_catalog.CATALOG:
            if last_cat is not None and cat != last_cat:
                self.insertSeparator(self.count())
            last_cat = cat
            self._add(font_catalog.SYSTEM_LABELS.get(family, family), family, cat)
        self.insertSeparator(self.count())
        self._add("Other installed font\u2026", self.OTHER, "")
        self._select(value)
        self.currentIndexChanged.connect(self._picked)
        opt = _opt(self)
        widest = max(self.itemDelegate().sizeHint(opt, self.model().index(i, 0)).width() for i in range(self.count()))
        self.view().setMinimumWidth(widest + 24)
        self.setMinimumContentsLength(12)

    def _add(self, label, family, cat, at=None) -> None:
        i = self.count() if at is None else at
        self.insertItem(i, label, family)
        self.setItemData(i, family, Qt.UserRole + 1)
        self.setItemData(i, cat, Qt.UserRole + 2)
        f = QFont(family if family != self.OTHER else "")
        self.setItemData(i, f, Qt.FontRole)

    def _select(self, family: str) -> None:
        i = self.findData(family)
        if i < 0:                                      # a system font picked earlier: list it
            i = self.count() - 2
            self._add(family, family, "Installed", at=i)
        self.blockSignals(True)
        self.setCurrentIndex(i)
        self.blockSignals(False)
        self._prev = i
        self._show_in_font(family)

    def _show_in_font(self, family: str) -> None:
        f = QFont(family)
        f.setPixelSize(15)
        self.setFont(f)

    def family(self) -> str:
        return self.currentData()

    def _picked(self, i: int) -> None:
        fam = self.itemData(i)
        if fam == self.OTHER:
            from PySide6.QtGui import QFontDatabase
            from PySide6.QtWidgets import QInputDialog
            fams = sorted(set(QFontDatabase.families()))
            cur = self.itemData(self._prev) or "sans-serif"
            name, ok = QInputDialog.getItem(self, "Other font", "Installed fonts (OBS's browser has to have it too):",
                                            fams, fams.index(cur) if cur in fams else 0, True)
            if not ok or not name.strip():
                self.blockSignals(True)
                self.setCurrentIndex(self._prev)
                self.blockSignals(False)
                return
            self._select(name.strip())
            fam = name.strip()
        else:
            self._prev = i
            self._show_in_font(fam)
        self._on_change(fam)


def _opt(widget):
    from PySide6.QtWidgets import QStyleOptionViewItem
    o = QStyleOptionViewItem()
    o.initFrom(widget)
    return o


class StyleForm(QWidget):
    """One widget per schema option; calls on_change(key, value)."""

    def __init__(self, schema, values: dict, on_change, parent=None):
        super().__init__(parent)
        self.widgets = {}
        theme = Theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        css = combo_box_stylesheet(theme_config.get_settings())
        for group, opts in schema:
            lay.addWidget(section_title(group))
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignLeft)
            for key, label, typ, default, extra in opts:
                val = values.get(key, default)
                if typ == "bool":
                    w = CustomCheckBox("")
                    w.setChecked(bool(val))
                    w.toggled.connect(lambda v, k=key: on_change(k, bool(v)))
                elif typ == "color":
                    w = _ColorButton(str(val))
                    w.changed.connect(lambda v, k=key: on_change(k, v))
                elif typ == "int":
                    w = CustomSpinBox()
                    w.setRange(*extra)
                    w.setValue(int(val))
                    w.valueChanged.connect(lambda v, k=key: on_change(k, int(v)))
                elif typ == "float":
                    w = CustomDoubleSpinBox()
                    w.setRange(*extra)
                    w.setSingleStep(0.5)
                    w.setValue(float(val))
                    w.valueChanged.connect(lambda v, k=key: on_change(k, float(v)))
                elif typ == "font":
                    w = FontPicker(str(val), lambda v, k=key: on_change(k, v))
                elif typ == "choice":
                    w = QComboBox()
                    w.setStyleSheet(css)
                    for v, lab in extra:
                        w.addItem(lab, v)
                    idx = [v for v, _l in extra].index(val) if val in [v for v, _l in extra] else 0
                    w.setCurrentIndex(idx)
                    w.currentIndexChanged.connect(lambda _i, k=key, w=w: on_change(k, w.currentData()))
                else:
                    w = CustomLineEdit(str(val))
                    w.textChanged.connect(lambda v, k=key: on_change(k, v))
                lbl = QLabel(label)
                lbl.setStyleSheet(label_style(theme.text()))
                lbl.setWordWrap(True)
                lbl.setMinimumWidth(240)
                if typ == "choice":
                    w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                    w.setMinimumContentsLength(14)
                form.addRow(lbl, w)
                self.widgets[key] = w
            lay.addLayout(form)
        lay.addStretch(1)


class _Preview(QWidget):
    """Checkerboard (= transparent) + the page drawn with the real painter."""

    def __init__(self, page: str, style: dict, parent=None, scene=None):
        super().__init__(parent)
        self.page, self.style_ = page, dict(style)
        self.scene = scene
        self.clock = time.perf_counter
        self.state = demo_state(self.clock)
        self.setMinimumSize(420, 220)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update)
        self.timer.start()

    def set_style(self, style: dict) -> None:
        self.style_ = dict(style)
        self.update()

    def paintEvent(self, _e) -> None:
        if self.clock() - getattr(self, "_born", 0) > 1.6:       # replay the demo movement
            self._born = self.clock()
            self.state = demo_state(self.clock)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setClipPath(rounded_rect_path(r, 10))
        sq = 12
        for yy in range(0, self.height(), sq):
            for xx in range(0, self.width(), sq):
                p.fillRect(xx, yy, sq, sq, QColor("#3a3a3a") if (xx // sq + yy // sq) % 2 else QColor("#2e2e2e"))
        now = self.clock()
        if self.page == "full":
            from kbm_paint import layout_for, paint_kbm, picture_size
            lay = kl.build_scene(self.scene or kl.DEFAULT_SCENE)
            u = min((r.width() - 16) / lay["w"], (r.height() - 16) / lay["h"], float(self.style_["unit"]))
            w, h = picture_size(lay, self.style_, u)
            paint_kbm(p, lay, self.style_, self.state, now, unit=u, origin=((r.width() - w) / 2, (r.height() - h) / 2))
        elif self.page == "simple":
            from overlay_render import paint_simple
            h = max(20.0, self.style_["font_px"] * 1.6)
            p.translate(0, (r.height() - h) / 2)
            paint_simple(p, self.state, self.style_, now, r.width(), h)
        else:
            side = min(r.width(), r.height(), float(self.style_["size"]))
            box = QRectF((r.width() - side) / 2, (r.height() - side) / 2, side, side)
            bg = qcolor(self.style_.get("background", "#00000000"))
            if bg.alpha():
                p.fillRect(box, bg)
            from kbm_paint import paint_motion
            paint_motion(p, box, self.state, self.style_, now, self.style_.get("motion", "mousepad"), "preview",
                         self.style_.get("center", "head"))


SIDEBAR_WIDTH = 560      # the Customize dialog's options column (wide enough for every label)


class CustomizeDialog(QDialog):
    TITLES = {"full": "Input overlay (keyboard, mouse, controller)", "simple": "Simple input list",
              "movement": "Mouse movement"}
    SCHEMAS = {"full": oc.STYLE_SCHEMA, "simple": oc.SIMPLE_SCHEMA, "movement": oc.MOVEMENT_SCHEMA}
    KEYS = {"full": "style", "simple": "simple_style", "movement": "movement_style"}

    def __init__(self, section: "OverlaySection", page: str):
        super().__init__(section)
        self.section, self.page = section, page
        self.setWindowTitle("Customize " + self.TITLES[page])
        theme = Theme()
        pal = self.palette()
        pal.setColor(self.backgroundRole(), theme.page_background())
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        key = self.KEYS[page]
        root = QHBoxLayout(self)
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)     # the sidebar is sized to fit instead
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 12, 0)
        self.elements = None
        if page == "full":
            from element_panel import ElementPanel
            side_lay.addWidget(section_title("Layout"))
            side_lay.addWidget(dim_label("What's shown and how it looks. Drag things around with Edit layout "
                                         "on the Input Visualizer page."))
            self.elements = ElementPanel(section.cfg["scene"], on_change=self._scene_changed, fixed_width=False)
            side_lay.addWidget(self.elements)
        self.form = StyleForm(self.SCHEMAS[page], section.cfg[key], self._changed)
        side_lay.addWidget(self.form)
        scroll.setWidget(side)
        need = max(side.sizeHint().width(), self.form.sizeHint().width() + 12)
        scroll.setFixedWidth(max(SIDEBAR_WIDTH, need + scroll.verticalScrollBar().sizeHint().width() + 8))
        root.addWidget(scroll)
        right = QVBoxLayout()
        right.addWidget(dim_label("Preview (checkers = transparent). Changes reach OBS right away."))
        self.preview = _Preview(page, section.cfg[key], scene=section.cfg.get("scene"))
        right.addWidget(self.preview, 1)
        reset = CustomButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        close = CustomButton("Done")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch(1)
        row.addWidget(close)
        right.addLayout(row)
        root.addLayout(right, 1)
        self.resize(1260, 720)

    def _scene_changed(self) -> None:
        scene = self.elements.scene
        self.preview.scene = scene
        self.preview.update()
        self.section.set_scene(scene)
        if self.section.on_scene_edited:
            import copy
            self.section.on_scene_edited(copy.deepcopy(scene))

    def _changed(self, k, v) -> None:
        key = self.KEYS[self.page]
        if k == "screen_height":
            self.section.cfg["screen_height_user"] = True
        self.section.cfg[key][k] = v
        self.preview.set_style(self.section.cfg[key])
        self.section.changed(restart=False)

    def _reset(self) -> None:
        key = self.KEYS[self.page]
        self.section.cfg[key] = dict(oc.DEFAULTS[key])
        self.section.changed(restart=False)
        self.accept()


# ---------------------------------------------------------------------------
# The section on the Input Visualizer page
# ---------------------------------------------------------------------------
class _Relay(QObject):
    done = Signal(str, str)      # (what, message)


class OverlaySection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = oc.load()
        self.on_scene_edited = None          # callback(scene): the Customize dialog's element list changed it
        if not self.cfg.get("screen_height_user"):              # the movement views' scale: this screen
            scr = QApplication.primaryScreen()
            if scr is not None:
                hpx = int(scr.size().height() * scr.devicePixelRatio())
                if hpx > 0:
                    self.cfg["style"]["screen_height"] = hpx
                    self.cfg["movement_style"]["screen_height"] = hpx
        self._saved_sig = self._restart_sig()
        theme = Theme()
        self.relay = _Relay()
        self.relay.done.connect(self._async_done)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = section_title("OBS & replay overlay")
        mark_output(title)
        lay.addWidget(title)
        lay.addWidget(dim_label(
            "Shows your real keyboard and mouse in OBS (a Browser Source pointed at a local page), and can keep a "
            "rolling record of your input the length of OBS's replay buffer, for afterglow to draw onto saved clips. "
            "Input comes from the daemon, so it works whichever window has focus."))

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self._save_and_restart)
        self._style_timer = QTimer(self)
        self._style_timer.setSingleShot(True)
        self._style_timer.setInterval(150)
        self._style_timer.timeout.connect(self._save_only)

        # --- full (keyboard + mouse), or split into pieces
        self.full_toggle, self.full_port, self.full_url = self._page_row(
            lay, "Expose Input Visualizer to OBS", "full", "port", "full",
            "The visualizer's arrangement (keyboard, mouse, controller, movement views), with hold timers.")
        srow = QHBoxLayout()
        srow.addSpacing(28)
        self.split_toggle = CustomCheckBox("Each element as its own OBS source")
        self.split_toggle.setToolTip("Every element of the arrangement (keyboard, mouse, controller, movement views) "
                                     "also gets its own page, so OBS -- and afterglow -- can place and size each one.")
        self.split_toggle.setChecked(bool(self.cfg["full"].get("element_sources")))
        self.split_toggle.toggled.connect(lambda v: self._set("full", "element_sources", bool(v)))
        srow.addWidget(self.split_toggle)
        srow.addStretch(1)
        lay.addLayout(srow)
        self.element_box = QVBoxLayout()
        lay.addLayout(self.element_box)
        lay.addWidget(dim_label("What's shown and where: \"Edit layout\" above the visualizer (add the controller, "
                                "a different keyboard size or mouse, Comet / Mousepad / Joystick movement)."))

        # --- simple
        self.simple_toggle, self.simple_port, self.simple_url = self._page_row(
            lay, "Simple input visualizer", "simple", "port", "simple",
            "Just a line of text: what's held right now (Ctrl + Shift + A + LMB).")
        mrow = QHBoxLayout()
        mrow.addSpacing(28)
        self.mouse_toggle = CustomCheckBox("Mouse movement (a separate page with one movement view)")
        self.mouse_toggle.setChecked(bool(self.cfg["simple"].get("mouse_movement")))
        self.mouse_toggle.toggled.connect(self._mouse_toggled)
        mrow.addWidget(self.mouse_toggle)
        self.mouse_port = CustomSpinBox()
        self.mouse_port.setRange(1024, 65535)
        self.mouse_port.setValue(int(self.cfg["simple"]["mouse_port"]))
        self.mouse_port.valueChanged.connect(lambda v: self._set("simple", "mouse_port", int(v)))
        mrow.addWidget(QLabel("port"))
        mrow.addWidget(self.mouse_port)
        self.mouse_url = self._url_widgets(mrow, "movement")
        mrow.addStretch(1)
        lay.addLayout(mrow)
        lay.addWidget(dim_label("Everywhere: your own input is drawn in the input color (orange) and what macros "
                                "press or move in the output color (blue) -- both changeable in Customize."))

        # --- replay
        rrow = QHBoxLayout()
        self.replay_toggle = ToggleSwitch(bool(self.cfg["replay"]["enabled"]))
        self.replay_toggle.toggled.connect(lambda v: self._set("replay", "enabled", bool(v)))
        rrow.addWidget(self.replay_toggle)
        lbl = QLabel("Layered Replay Buffer")
        lbl.setStyleSheet(label_style(theme.text(), "font-weight: bold;"))
        rrow.addWidget(lbl)
        rrow.addSpacing(16)
        rrow.addWidget(QLabel("if OBS can't be asked, keep"))
        self.fallback = CustomSpinBox()
        self.fallback.setRange(5, 3600)
        self.fallback.setSuffix(" s")
        self.fallback.setValue(int(self.cfg["replay"]["fallback_seconds"]))
        self.fallback.valueChanged.connect(lambda v: self._set("replay", "fallback_seconds", int(v), restart=False))
        rrow.addWidget(self.fallback)
        rrow.addWidget(QLabel("+ margin"))
        self.extra = CustomSpinBox()
        self.extra.setRange(0, 120)
        self.extra.setSuffix(" s")
        self.extra.setValue(int(self.cfg["replay"]["extra_seconds"]))
        self.extra.valueChanged.connect(lambda v: self._set("replay", "extra_seconds", int(v), restart=False))
        rrow.addWidget(self.extra)
        rrow.addStretch(1)
        lay.addLayout(rrow)
        lay.addWidget(dim_label(
            f"Keeps your input for as long as OBS's replay buffer (asked over OBS's WebSocket every 30 s) in "
            f"{oc.buffer_file()} -- in memory, never on disk. afterglow reads it to put the overlay on saved clips."))

        # --- OBS connection
        orow = QHBoxLayout()
        orow.addWidget(QLabel("OBS WebSocket"))
        self.obs_host = CustomLineEdit(self.cfg["obs"]["host"])
        self.obs_host.setFixedWidth(130)
        self.obs_host.textChanged.connect(lambda v: self._set("obs", "host", v.strip(), restart=False))
        orow.addWidget(self.obs_host)
        self.obs_port = CustomSpinBox()
        self.obs_port.setRange(1, 65535)
        self.obs_port.setValue(int(self.cfg["obs"]["port"]))
        self.obs_port.valueChanged.connect(lambda v: self._set("obs", "port", int(v), restart=False))
        orow.addWidget(self.obs_port)
        self.obs_pw = CustomLineEdit(self.cfg["obs"].get("password", ""))
        self.obs_pw.setEchoMode(CustomLineEdit.Password)
        self.obs_pw.setPlaceholderText("password (Tools > WebSocket Server Settings)")
        self.obs_pw.setFixedWidth(260)
        self.obs_pw.textChanged.connect(lambda v: self._set("obs", "password", v, restart=False))
        orow.addWidget(self.obs_pw)
        test = CustomButton("Test")
        test.setToolTip("Connect to OBS and read the replay buffer length.")
        test.clicked.connect(self.test_obs)
        orow.addWidget(test)
        self.add_btn = CustomButton("Add to OBS")
        self.add_btn.setToolTip("Create (or update) Browser Sources for the pages that are on, in OBS's current scene.")
        self.add_btn.clicked.connect(self.add_to_obs)
        orow.addWidget(self.add_btn)
        orow.addStretch(1)
        lay.addLayout(orow)
        self.obs_msg = dim_label("")
        lay.addWidget(self.obs_msg)
        self.status = dim_label("")
        lay.addWidget(self.status)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()
        self._sync_urls()
        self.refresh_status()

    # -- building blocks --------------------------------------------------------
    def _page_row(self, lay, title, section: str, port_key: str, customize: str, hint: str):
        theme = Theme()
        row = QHBoxLayout()
        tog = ToggleSwitch(bool(self.cfg[section]["enabled"]))
        tog.toggled.connect(lambda v: self._set(section, "enabled", bool(v)))
        row.addWidget(tog)
        lbl = QLabel(title)
        lbl.setStyleSheet(label_style(theme.text(), "font-weight: bold;"))
        lbl.setToolTip(hint)
        tog.setToolTip(hint)
        row.addWidget(lbl)
        row.addSpacing(8)
        row.addWidget(QLabel("port"))
        port = CustomSpinBox()
        port.setRange(1024, 65535)
        port.setValue(int(self.cfg[section][port_key]))
        port.valueChanged.connect(lambda v: self._set(section, port_key, int(v)))
        row.addWidget(port)
        url = self._url_widgets(row, None)
        cust = CustomButton("Customize…")
        cust.clicked.connect(lambda: CustomizeDialog(self, customize).exec())
        row.addWidget(cust)
        row.addStretch(1)
        lay.addLayout(row)
        return tog, port, url

    def _sub_row(self, lay, title, section, port_key, page):
        row = QHBoxLayout()
        row.addSpacing(56)
        row.addWidget(QLabel(title))
        row.addWidget(QLabel("port"))
        port = CustomSpinBox()
        port.setRange(1024, 65535)
        port.setValue(int(self.cfg[section][port_key]))
        port.valueChanged.connect(lambda v: self._set(section, port_key, int(v)))
        row.addWidget(port)
        url = self._url_widgets(row, None)
        row.addStretch(1)
        lay.addLayout(row)
        return port, url

    def _url_widgets(self, row, page):
        url = QLabel("")
        url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url.setFont(__import__("PySide6.QtGui", fromlist=["QFont"]).QFont("monospace"))
        row.addWidget(url)
        copy = CustomButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(url.text()))
        row.addWidget(copy)
        if page == "movement":
            cust = CustomButton("Customize…")
            cust.clicked.connect(lambda: CustomizeDialog(self, "movement").exec())
            row.addWidget(cust)
        url._copy = copy
        return url

    def _sync_urls(self) -> None:
        c = self.cfg
        on = oc.pages(c)
        for lbl, page, port in ((self.full_url, "full", c["full"]["port"]),
                                (self.simple_url, "simple", c["simple"]["port"]),
                                (self.mouse_url, "movement", c["simple"]["mouse_port"])):
            lbl.setText(f"http://127.0.0.1:{port}/")
            lbl.setEnabled(page in on)
            lbl._copy.setEnabled(page in on)
        self.split_toggle.setEnabled(bool(c["full"]["enabled"]))
        self.mouse_toggle.setEnabled(bool(c["simple"]["enabled"]))
        self.add_btn.setEnabled(bool(on))
        # one row per element when they're separate sources
        while self.element_box.count():
            it = self.element_box.takeAt(0)
            if it.layout():
                while it.layout().count():
                    w = it.layout().takeAt(0).widget()
                    if w:
                        w.setParent(None)
                        w.deleteLater()
        self.element_urls = {}
        for el_id, url in oc.element_urls(c).items():
            row = QHBoxLayout()
            row.addSpacing(56)
            row.addWidget(QLabel(el_id))
            lbl = self._url_widgets(row, None)
            lbl.setText(url)
            self.element_urls[el_id] = lbl
            row.addStretch(1)
            self.element_box.addLayout(row)

    def set_scene(self, scene: dict) -> None:
        """The visualizer's Edit mode changed the arrangement."""
        self.cfg["scene"] = scene
        self._sync_urls()
        self.changed(restart=False)

    # -- saving -------------------------------------------------------------------
    def _restart_sig(self):
        c = self.cfg
        return (tuple(sorted(oc.pages(c).items())), c["replay"]["enabled"])

    def _set(self, section, key, value, restart=True) -> None:
        self.cfg[section][key] = value
        self._sync_urls()
        self.changed(restart)
        self.refresh_status()

    def _mouse_toggled(self, v) -> None:
        self._set("simple", "mouse_movement", bool(v))

    def changed(self, restart: bool) -> None:
        if restart and self._restart_sig() != self._saved_sig:
            self._save_timer.start()
        else:
            self._style_timer.start()

    def _save_only(self) -> None:
        try:
            oc.save(self.cfg)
        except OSError as e:
            self.status.setText(f"Couldn't save overlay.json: {e}")

    def _save_and_restart(self) -> None:
        self._save_only()
        sig = self._restart_sig()
        if sig != self._saved_sig:
            self._saved_sig = sig
            ok, msg = cfg_mod.restart_daemon_service()
            self.status.setText("Daemon restarted to apply." if ok else msg)

    def flush(self) -> None:
        """Save anything pending (page hidden / app closing)."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_and_restart()
        elif self._style_timer.isActive():
            self._style_timer.stop()
            self._save_only()

    # -- status + OBS -------------------------------------------------------------
    def refresh_status(self) -> None:
        if not self.isVisible() and self.status.text():
            return
        try:
            st = json.loads(oc.status_file().read_text())
            fresh = time.time() - st.get("updated", 0) < 10
        except (OSError, ValueError):
            st, fresh = {}, False
        if not oc.helper_wanted(self.cfg):
            self.status.setText("Everything here is off.")
        elif not fresh:
            self.status.setText("Overlay helper not running yet (it starts with the daemon).")
        else:
            rp = st.get("replay", {})
            bits = ["Overlay helper running" + ("" if st.get("daemon_connected") else " (waiting for the daemon)")]
            if rp.get("enabled"):
                src = "from OBS" if rp.get("length_source") == "obs" else "fallback -- OBS not reachable"
                bits.append(f"replay buffer {rp.get('length_s', 0):g} s ({src})")
            bits.append(f"OBS: {st.get('obs')}")
            self.status.setText(" · ".join(bits))

    def _run_async(self, what, fn) -> None:
        def work():
            try:
                msg = fn()
            except Exception as e:  # noqa: BLE001
                msg = f"Failed: {e}"
            self.relay.done.emit(what, msg)
        threading.Thread(target=work, daemon=True).start()

    def _client(self):
        from obs_client import ObsClient
        o = self.cfg["obs"]
        return ObsClient(o["host"], o["port"], o.get("password", ""))

    def test_obs(self) -> None:
        self.flush()
        self.obs_msg.setText("Connecting to OBS…")

        def fn():
            with self._client() as c:
                secs = c.replay_buffer_seconds()
            return (f"Connected. Replay buffer: {secs:g} s." if secs
                    else "Connected, but OBS didn't report a replay buffer length (is the replay buffer enabled?).")
        self._run_async("test", fn)

    def add_to_obs(self) -> None:
        self.flush()
        from overlay_cli import obs_sources
        sources = obs_sources(self.cfg)
        self.obs_msg.setText("Adding to OBS…")

        def fn():
            with self._client() as c:
                done = c.add_browser_sources(sources)
            return "In OBS: " + ", ".join(f"{n} ({how})" for n, how in done)
        self._run_async("add", fn)

    def _async_done(self, _what, msg) -> None:
        self.obs_msg.setText(msg)
