import sys, time
from dataclasses import replace
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QComboBox
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QEnterEvent, QColor, QPixmap
app = QApplication([])
from PySide6.QtGui import QCursor
QCursor.setPos(4000, 4000)
import ui_kit
from ui_kit import theme_config, ThemeSettings
fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)
def pump(s):
    end = time.perf_counter() + s
    while time.perf_counter() < end: app.processEvents(); time.sleep(0.004)
def px(w, x, y):
    assert not w.underMouse(), f"{type(w).__name__} is hovered -- sample would include hover tint"
    return w.grab().toImage().pixelColor(x, y).name()

# 1. no provider -> placeholder monochrome
b = ui_kit.CustomButton("OK"); b.resize(120, 40); b.show(); pump(0.05)
# Puppetry: default buttons + nav/segment buttons use color_general (purple,
# "neither input nor output") instead of accent/highlight.
check(px(b, 22, 20) == ThemeSettings().color_general, f"placeholder general color on CustomButton ({px(b,22,20)})")

# 2. per-app palette through provider (Conduit-ish example)
conduit = ThemeSettings(color_accent="#7a3cff", color_surface="#241a3a", color_app_background="#140f22",
                        color_page_background="#0d0a17", color_highlight="#c04cff", color_text="#f0e6ff",
                        color_general="#7a3cff")
theme_config.set_settings_provider(lambda: conduit)
b2 = ui_kit.CustomButton("OK"); b2.resize(120, 40); b2.show(); pump(0.05)
check(px(b2, 22, 20) == "#7a3cff", "provider palette reaches CustomButton")
seg = ui_kit.SegmentButton(text="Page", position="left"); seg.setChecked(True); seg.resize(120, 40); seg.show(); pump(0.05)
check(px(seg, 60, 5) == "#7a3cff", f"SegmentButton checked uses the general color ({px(seg,60,5)})")
img = seg.grab().toImage()
fill = "#7a3cff"
corners = {k: img.pixelColor(x, y).name() for k, (x, y) in
           {"TL": (0, 0), "TR": (119, 0), "BL": (0, 39), "BR": (119, 39)}.items()}
check(corners["TR"] == fill and corners["BR"] == fill and corners["TL"] != fill and corners["BL"] != fill,
      f"position='left': right corners sharp, left corners rounded {corners}")
le = ui_kit.CustomLineEdit("hi"); le.resize(200, 36); le.show(); pump(0.05)
check(px(le, 100, 18) == "#241a3a", "CustomLineEdit fill = surface")
sb = ui_kit.CustomSpinBox(); sb.resize(120, 34); sb.show(); pump(0.05)
check(px(sb, 30, 17) == "#241a3a", "CustomSpinBox fill = surface")
cb = ui_kit.CustomCheckBox("Label"); cb.resize(150, 28); cb.show(); pump(0.05)
check(px(cb, 9, 14) == "#241a3a", f"CustomCheckBox box fill = surface ({px(cb,9,14)})")
cb.setChecked(True); pump(0.05)
check(px(cb, 9, 14) != "#241a3a", "checkmark asset loads and draws when checked")
dlg = ui_kit.CustomMessageDialog("Title", "Body"); dlg.resize(320, 140); dlg.show(); pump(0.05)
check(px(dlg, 160, 70) == "#0d0a17", "CustomMessageDialog fill = page_background")
combo = QComboBox(); combo.setStyleSheet(ui_kit.combo_box_stylesheet(ui_kit.get_settings()))
check("#241a3a" in combo.styleSheet() and "#7a3cff" in combo.styleSheet(), "combo stylesheet uses surface/accent")

# 3. app_theme off -> live system palette
theme_config.set_settings_provider(lambda: replace(conduit, app_theme_enabled=False))
b3 = ui_kit.CustomButton("OK"); b3.resize(120, 40); b3.show(); pump(0.05)
from PySide6.QtGui import QPalette
check(px(b3, 22, 20) == app.palette().color(QPalette.Highlight).name(), "app_theme off -> system Highlight")
theme_config.set_settings_provider(lambda: conduit)

# 4. rounding off -> square corners
theme_config.set_settings_provider(lambda: replace(conduit, rounded_corners_enabled=False))
b4 = ui_kit.CustomButton("OK"); b4.resize(120, 40); b4.show(); pump(0.05)
check(px(b4, 0, 0) == "#7a3cff", "rounding off -> corner pixel is button fill")
theme_config.set_settings_provider(lambda: conduit)

# 5. pulse on every pulsing class, real events
for name, w in [("CustomButton", b2), ("SegmentButton", seg), ("CustomCheckBox", cb),
                ("CustomRadioButton", ui_kit.CustomRadioButton("r")), ("CollapseToggle", ui_kit.CollapseToggleButton())]:
    if not w.isVisible(): w.resize(120, 30); w.show(); pump(0.05)
    c = w.rect().center()
    app.sendEvent(w, QEnterEvent(c.toPointF(), w.mapToGlobal(c).toPointF(), w.mapToGlobal(c).toPointF()))
    pump(0.25); hv = w._pulse.scale
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, c); pump(0.25); pr = w._pulse.scale
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, c); pump(0.5); st = w._pulse.scale
    check(abs(hv-.96)<1e-3 and abs(pr-.90)<1e-3 and abs(st-.96)<1e-3, f"pulse {name}: {hv:.2f}/{pr:.2f}/{st:.2f}")


# 5b. release bounce never crosses the widget edge (it used to hit 1.04,
#     which clipped the rounded corners off at the peak)
tall = ui_kit.SegmentButton(text="Nav", position="full"); tall.setChecked(True); tall.resize(100, 340); tall.show(); pump(0.05)
c = tall.rect().center()
app.sendEvent(tall, QEnterEvent(c.toPointF(), tall.mapToGlobal(c).toPointF(), tall.mapToGlobal(c).toPointF())); pump(0.25)
QTest.mousePress(tall, Qt.LeftButton, Qt.NoModifier, c); pump(0.25)
QTest.mouseRelease(tall, Qt.LeftButton, Qt.NoModifier, c)
peak = 0; end = time.perf_counter() + 0.45
while time.perf_counter() < end:
    app.processEvents(); peak = max(peak, tall._pulse.scale); time.sleep(0.003)
check(0.99 <= peak <= 1.0 + 1e-6 and abs(tall._pulse.scale - 0.96) < 1e-3,
      f"release bounce peaks at the widget edge ({peak:.3f}) and settles at hover size")
# if someone raises the overshoot, headroom keeps the peak inside the rect
tall._pulse._overshoot_scale = 1.04
tall._pulse.scale = 1.04; tall._pulse._widget.update(); pump(0.02)
img = tall.grab().toImage()
tall_fill = img.pixelColor(50, 40).name()  # still hovered from above, and clear of the centered label: compare to its own fill
check(img.pixelColor(0, 0).name() != tall_fill and img.pixelColor(50, 0).name() == tall_fill,
      "overshoot > 1.0: headroom keeps rounded corners visible at the peak")
seg2 = ui_kit.SegmentButton(text="L", position="left"); seg2.setChecked(True); seg2.resize(80, 40); seg2.show()
seg2._pulse._overshoot_scale = 1.04; seg2._pulse.scale = 1.0; pump(0.05)
img = seg2.grab().toImage()
check(img.pixelColor(79, 20).name() == "#7a3cff" and img.pixelColor(0, 20).name() != "#7a3cff",
      "overshoot > 1.0: touching edge stays flush at rest, rounded edge insets")

# 6. theme editor: apply + revert, invalid color rejected
defaults = ThemeSettings(color_accent="#111111")
cur = replace(conduit)
ed = ui_kit.ThemeEditorGroup(cur, defaults); ed.show(); pump(0.05)
check(ed._colors["color_accent"].text() == "#7a3cff", "editor loads current values")
ed._colors["color_surface"].setText("#ABCDEF"); ed._colors["color_text"].setText("notacolor")
target = replace(conduit); bad = ed.apply_to(target)
check(target.color_surface == "#abcdef" and target.color_text == "#f0e6ff" and bad == ["color_text"],
      "apply_to writes valid colors, skips + reports invalid")
check(cur.color_surface == "#241a3a", "editor never mutates `current`")
ed.revert_colors()
check(ed._colors["color_accent"].text() == "#111111", "Revert Colors loads app defaults")

# 7. no unscoped stylesheets (cascade gotcha)
import re
bad_ss = []
for w in app.allWidgets():
    ss = w.styleSheet().strip()
    if ss and not re.match(r"^\s*[A-Za-z]", ss): bad_ss.append(type(w).__name__)
check(not bad_ss, f"no unscoped stylesheets ({bad_ss})")

# 8. SmoothScrollArea uses CustomScrollBar
sa = ui_kit.SmoothScrollArea()
check(isinstance(sa.verticalScrollBar(), ui_kit.CustomScrollBar), "SmoothScrollArea installs CustomScrollBar")
print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
