# Custom UI theming — porting guide for Conduit and Puppetry

This explains how afterglow's custom UI theming works, and how to bring
the same system into **Conduit** and **Puppetry**. It is written for a
Claude chat that has never seen afterglow's code. Everything needed is
in the `ui_kit/` folder next to this file. `demo_app.py` is a small
runnable example of an app wired up the way afterglow is.

## Ground rules (read first)

- **All three apps are PySide6 (Qt).** Puppetry's editor is being ported
  from GTK4 to Qt; build Puppetry's new UI on this kit, not GTK.
- **Copy `ui_kit/` into each repo** (e.g. `conduit/gui/ui_kit/`,
  `puppetry/gui/ui_kit/`). It is not a shared package. Each app owns its
  copy and can tweak it.
- **Adopt the full kit**, not just buttons (list under "Widget catalog").
- **Colors are editable in each app's own settings**, and **each app has
  its own colors**. Do not reuse afterglow's colors.
- **Colors:** assume colors will be provided by the user and will not be
  completely consistent across the apps. If no colors are specified,
  just use a placeholder monochromatic scheme. (The kit's built-in
  `ThemeSettings()` defaults are exactly that placeholder scheme.)
- Changing UI toolkit or backend requires explicit confirmation before
  any work starts.

---

## 1. How it fits together

```
 app config file (TOML etc.)
        │  app loads + caches it
        ▼
 ThemeSettings  ──►  set_settings_provider(fn)   (once, at startup)
        │
        ▼
   get_settings()  ◄── every kit widget calls this in __init__
        │
        ▼
      Theme  ──►  accent() / surface() / app_background() / ...
        │            (fixed hex colors, OR the live system palette)
        ▼
 custom-painted widgets: each one draws itself in paintEvent with
 QPainter, using rounded_rect_path() for shapes and PressPulse for motion
```

**Four layers, each with one job:**

1. **`ThemeSettings`** (`theme_config.py`): a plain dataclass holding
   every theming value: two master switches, corner radius, padding,
   and seven color roles. The app stores one of these in its own config.
2. **The provider** (`set_settings_provider`): the only link between the
   kit and the app. The kit never imports the app's config module. The
   app hands the kit a function that returns the current `ThemeSettings`.
3. **`Theme`** (`theme.py`): turns settings into `QColor`s. **Every
   themed color goes through a `Theme` accessor.** Never call
   `QColor("#...")` or `QApplication.palette()` directly in a widget.
   This is what lets one switch flip the whole app between the app
   palette and the system palette, and lets later visual passes (subtle
   gradients are planned) touch one file.
4. **Widgets**: each fully replaces native painting in its own
   `paintEvent`. They keep Qt's real behavior (clicks, focus, text
   editing, scrolling, popups); only the drawing is custom.

### The two master switches

| Setting | Afterglow's name | Meaning |
|---|---|---|
| `custom_widgets_enabled` | "Custom Buttons" | **Whether** to custom-paint at all. Off = use plain native Qt widgets. |
| `app_theme_enabled` | "Afterglow Theme" | **Which colors** custom widgets use. On = the hex colors in settings. Off = sampled live from the system/KDE palette. |

They're independent. With custom widgets off, the theme switch has
nothing to recolor. Kit widgets do **not** check `custom_widgets_enabled`
themselves; the app decides which class to build, e.g.
`CustomButton(...) if theme.custom_widgets_enabled else QPushButton(...)`.
It's fine to wire this up late, or only for the main widgets.

---

## 2. Color roles

Role names are shared across apps. **Values are per app.** When colors
are provided, map them onto these roles. If more or fewer colors are
provided than there are roles, ask which role each one is for rather
than guessing, and put a placeholder monochrome value in any role left
unassigned.

| Field | `Theme` accessor | Used for | System-palette fallback |
|---|---|---|---|
| `color_accent` | `accent()`, `button_color()` | Button fills, outlines on text fields/spinboxes/checkboxes/group boxes, scrollbar handle, spinbox arrows, collapse toggle | `Highlight` |
| `color_surface` | `surface()` | Raised panels/cards, text field + spinbox + checkbox fill, scrollbar track, combo box fill | `AlternateBase` |
| `color_app_background` | `app_background()` | The main window's own background | `Window` |
| `color_page_background` | `page_background()` | Each content page's background, dialog fill | `Base` |
| `color_highlight` | `highlight()` | Navigation buttons (`SegmentButton`: sidebar and page tabs) | `Mid` |
| `color_text` | `text()` | Body text, labels, checkbox/dialog text | `WindowText` |
| `color_text_outline` | `text_outline()` | `OutlinedLabel` stroke (always the configured value) | none |

Placeholder monochrome defaults (what `ThemeSettings()` returns):
accent `#4a4a4a`, surface `#2b2b2b`, app background `#1e1e1e`, page
background `#161616`, highlight `#6a6a6a`, text `#e6e6e6`, text outline
`#101010`.

**Text on a filled shape** is never a fixed color. Use
`contrast_text(fill)`, which picks near-black or near-white by
perceptual luminance, so text stays readable whatever colors are picked.

**State shading** is derived from the base color, never separate
settings: pressed `darker(125)`, hovered `lighter(112..115)`, unchecked
nav button `darker(140)`, loading `darker(150)`, page outline
`darker(115)` (15% darker than that page's own background).

**For afterglow context only** (don't copy these values), afterglow maps
accent → buttons + card info box, surface → the video card body,
page background → the Library grid, highlight → sidebar/Local/Uploaded
buttons (its "turquoise"), and derives its app background as a
brighter, more saturated variant of the library color. Colors have been
re-picked many times. Expect the same in these apps.

---

## 3. Shape and spacing rules

**Rounded corners** (`rounded_rect.py`):
- `rounded_rect_path(rect, radius, top_left=..., top_right=..., bottom_left=..., bottom_right=...)`
  is the only way anything gets rounded. Don't use
  `QPainterPath.addRoundedRect`; it's plain circular and looks
  different.
- The curve is "smooth", not a quarter circle: each corner is a cubic
  Bézier with `SMOOTH_KAPPA = 0.68` (0.5523 would be circular). The
  target look is "Apple style", meaning it eases into the curve. True
  squircle math is explicitly **not** needed.
- Radius comes from settings (default 24px) and is clamped to half the
  shape's shorter side, so small widgets become pills rather than
  breaking. Some widgets fall back to a small radius (6–8px) when
  rounding is off, so they're never fully square. That's intentional.
- **Never round a corner that touches another element.** Two buttons
  sharing an edge have square corners on that edge. `SegmentButton`'s
  `position` argument ("left", "right", "middle", "top", "bottom",
  "full") encodes this.
- Window corners are **not** rounded by the app. That's left to the
  KDE/window-manager theme. Don't build a frameless rounded main window.
- Images get rounded with `round_pixmap_corners(pixmap, radius)`, which
  bakes the rounding into a copy.

**Padding:** one setting, `ui_padding` (default 14px), used for every
gap: between cards, between a panel's edge and its content, between
sidebar buttons, and around the window's content. Don't add separate
per-area spacing settings. Read it from `Theme().padding`.

**Page outlines** (`page_outline.py`): every top-level page draws a 3px
line 15% darker than **its own** background around its edge.
`paint_page_outline(widget, that_pages_bg, skip_top=...)`. Skip the
edge that sits flush against something (e.g. a header directly above).
Give the page a 3px layout margin so child widgets don't paint over the
outline.

**Window-relative sizing** (`scaling.py`): 1920×1080 = scale 1.0.
`compute_scale(w, h)` returns `min(w/1920, h/1080)` clamped to 0.5–2.0.
Call it from the main window's `resizeEvent` and pass it to anything
with hand-tuned pixel sizes (fonts, timeline heights). The sidebar is
7% of window width, clamped to 64–140px, and its icons are sized from
the sidebar width.

---

## 4. Motion

All motion is eased. Nothing snaps.

**Press pulse** (`press_pulse.py`), on every clickable custom widget:

| Event | Scale | Duration | Easing |
|---|---|---|---|
| hover enter | 0.96 | 150ms | OutCubic |
| hover leave | 1.00 | 150ms | OutCubic |
| mouse down | 0.90 | 110ms | OutCubic |
| mouse up | bounce up to 1.00 (90ms, OutCubic), then settle | 180ms | InOutCubic |

It settles at 0.96 if the cursor is still over the widget (the usual
case, so the release visibly bounces 0.90 → 1.00 → 0.96), or 1.0 if it
left. It resets on hide/disable so nothing gets stuck shrunk.

**The bounce peaks at 1.00, the widget's own edge, on purpose.** It used
to overshoot to 1.04, but a widget can't paint outside its own rect, so
the overshoot clipped the rounded corners off at the peak. A tall
sidebar button's 24px corner curve measured about 4px, which reads as
square. `RELEASE_OVERSHOOT_SCALE` is still a knob. If it is raised above
1.0, `apply()` automatically reserves headroom (draws at
`scale / overshoot`) so corners stay intact, but every button then
*rests* that much smaller: about 7px per end on a 340px sidebar button
at 1.04, which visibly widens the gaps between sidebar buttons. The
1.00 cap was chosen so resting sizes and spacing don't change. Raising it
requires explicit confirmation first.

If a widget has sides that touch a neighbor (joined tabs, a tab sitting
on a panel), extend those sides by `self._pulse.touching_extension(rect)`
before drawing, so they stay flush if headroom is ever on.
`SegmentButton` already does this. It's zero while the overshoot is 1.0.

Adding it to a widget takes two lines:

```python
self._pulse = PressPulse(self)          # in __init__, right after super().__init__
...
painter = QPainter(self)
self._pulse.apply(painter)              # first thing in paintEvent
```

For a widget with a label next to an indicator (checkbox, radio),
pulse **only the indicator**, so the text doesn't wiggle:

```python
painter.save()
self._pulse.apply(painter, box_rect.center())
# ...draw the box/checkmark...
painter.restore()
# ...draw the label...
```

Every animation frame repaints, so **never smooth-scale a pixmap inside
`paintEvent` directly**. Use `scaled_cached(pixmap, w, h)`.

**Other motion:**
- `crossfade_to_index(stack, i)`: page switches fade the new page in.
  Use this for the sidebar → `QStackedWidget`.
- `reveal_from_point` / `animate_popup_from_point`: a popup or page
  appears to grow out of the button that opened it (220ms, OutCubic).
- `CollapseToggleButton`: its ">" rotates 0°→90° (eased) when expanding.
- `SmoothScrollArea`: wheel scrolling animates (220ms OutCubic, 90px per
  notch). Fast flicks extend the running animation instead of
  restarting it.

---

## 5. Widget catalog (`ui_kit/`)

| File | Class / function | Replaces | Notes |
|---|---|---|---|
| `custom_button.py` | `CustomButton` | `QToolButton`/`QPushButton` | Subclasses `QToolButton`, so `setMenu` + `InstantPopup` still work. `set_icon_pixmap()` draws an icon instead of text. `set_circular(d)` makes a circle. `set_fill_color()` / `set_outline()` override per button. |
| `segment_button.py` | `SegmentButton` | Tab bars, sidebar nav | Checkable. Put a set in an exclusive `QButtonGroup`. Icon or text. `set_icon_target_size()`, `set_loading()`. `position` controls which corners round. |
| `custom_checkbox.py` | `CustomCheckBox` | `QCheckBox` | Rounded box, surface fill, accent outline, checkmark image. Optional leading icon. |
| `custom_radio_button.py` | `CustomRadioButton` | `QRadioButton` | Circular version of the checkbox. |
| `custom_spinbox.py` | `CustomSpinBox`, `CustomDoubleSpinBox` | `QSpinBox`/`QDoubleSpinBox` | Native arrows hidden; custom arrow buttons with auto-repeat. |
| `custom_line_edit.py` | `CustomLineEdit` | `QLineEdit` | Draws its box, then lets `QLineEdit` draw text on a transparent background. |
| `custom_group_box.py` | `CustomGroupBox` | `QGroupBox` | **Always build its layout with `group.make_layout(QVBoxLayout)`**, not `QVBoxLayout(group)`. See pitfall 6. |
| `custom_combo_style.py` | `combo_box_stylesheet(get_settings())` | `QComboBox` look | A **scoped** QSS string for `QComboBox`. Not a fully custom dropdown. |
| `custom_scrollbar.py` | `CustomScrollBar` | `QScrollBar` | 2× default width, surface track, accent handle. Uses the style's own `subControlRect` so dragging and track clicks still behave normally. |
| `smooth_scroll_area.py` | `SmoothScrollArea` | `QScrollArea` | Eased wheel scrolling + `CustomScrollBar` installed automatically. |
| `custom_message_dialog.py` | `show_message(parent, title, text)` | `QMessageBox.information/warning/critical` | Frameless, rounded, themed. No severity icons. |
| `outlined_label.py` | `OutlinedLabel` | `QLabel` for text over busy fills | Stroke drawn **first**, fill **on top** (two passes). Cached to a pixmap. |
| `collapse_toggle_button.py` | `CollapseToggleButton` | Arrow-type `QToolButton` | Round, rotating ">". |
| `page_outline.py` | `paint_page_outline(...)` | none | See section 3. |
| `rounded_rect.py` | `rounded_rect_path`, `round_pixmap_corners` | none | See section 3. |
| `press_pulse.py` | `PressPulse`, `scaled_cached` | none | See section 4. |
| `scale_reveal.py` | `crossfade_to_index`, `reveal_from_point`, `animate_popup_from_point` | none | See section 4. |
| `scaling.py` | `compute_scale` | none | See section 3. |
| `theme_editor.py` | `ThemeEditorGroup` | none | Ready-made Settings section. See section 6. |
| `resources/` | `checkmark_icon.png` | none | Afterglow's checkmark image (tinted to afterglow). Swap it per app if a different one is provided. |

---

## 6. Wiring it into an app (checklist)

1. **Copy `ui_kit/`** into the app's GUI package. Ship
   `ui_kit/resources/*.png` as package data (see
   `ui_kit/resources/__init__.py`) and make sure the NixOS flake
   includes it.
2. **Add a `ThemeSettings` to the app's config** (e.g. a `[theme]` TOML
   table). Define the app's own defaults. Until colors are provided,
   use the kit's placeholder values:
   ```python
   APP_THEME_DEFAULTS = ThemeSettings()   # replace color_* with the provided colors
   ```
3. **Install the provider at startup, before building any widget:**
   ```python
   from .ui_kit import theme_config
   theme_config.set_settings_provider(lambda: app_config.load_readonly().theme)
   ```
   The provider is called very often. It must return a cached object
   (see pitfall 1).
4. **Main window background via `QPalette`**, never
   `setStyleSheet("background: ...")` (pitfall 2). In the same palette,
   **set `WindowText`, `Text` and `ButtonText` to `theme.text()`** so
   plain `QLabel`s are readable (pitfall 3). See `demo_app.py`.
5. **Swap widgets**: `QPushButton`/`QToolButton` → `CustomButton`, tab
   bars / nav → `SegmentButton` in an exclusive `QButtonGroup`,
   `QCheckBox` → `CustomCheckBox`, `QGroupBox` → `CustomGroupBox` +
   `make_layout`, `QLineEdit` → `CustomLineEdit`, spinboxes →
   `CustomSpinBox`, `QScrollArea` → `SmoothScrollArea`, `QMessageBox` →
   `show_message`, combo boxes → `setStyleSheet(combo_box_stylesheet(get_settings()))`.
6. **Pages:** each top-level page is wrapped in a `SmoothScrollArea`,
   paints its own background, and calls `paint_page_outline`. Use
   `crossfade_to_index` for switching.
7. **Settings page:** add a `ThemeEditorGroup(current=cfg.theme, defaults=APP_THEME_DEFAULTS)`.
   On Save: `bad = editor.apply_to(cfg.theme)`, save the config, then
   warn about any `bad` (invalid colors are skipped, not saved). Pass
   `labels={...}` to rename rows in the app's own terms.
8. **App-specific custom widgets** (a Conduit routing node, a Puppetry
   timeline): paint them the same way. Take colors from `Theme`, shapes
   from `rounded_rect_path`, padding from `Theme().padding`, and add
   `PressPulse` if they're clickable.

### Live restyle

There isn't one. Widgets read settings when they're constructed. After
saving theme settings, tell the user to reopen the app (afterglow does),
or rebuild the affected pages. Don't try to broadcast restyle signals.

---

## 7. Pitfalls afterglow already hit (don't repeat these)

Every one of these was a real bug, several took multiple rounds to find.

1. **Config reads must be cached.** Afterglow's `config.load()`
   re-parsed TOML on every call, and kit widgets call it from
   constructors and `paintEvent`s: 400+ calls per Library refresh, over
   half the refresh time. Afterglow now caches and only re-parses when
   the file's mtime/size changes. It has two entry points:
   `load()` (a deep copy, safe to mutate and save) and `load_readonly()`
   (the shared cached object, used by the provider). Never mutate what
   `load_readonly()` returns.
2. **Never use an unscoped stylesheet.** `setStyleSheet("background-color: ...")`
   without a type selector cascades to **every descendant widget** and
   breaks their custom painting on a real desktop. It did **not**
   reproduce in the offscreen test environment, which is why it took
   several screenshot rounds to find. Use `QPalette` for backgrounds.
   Any stylesheet that is written must start with a selector
   (`QLineEdit { ... }`). Afterglow has a test that walks the widget
   tree and fails on unscoped stylesheets.
3. **Plain `QLabel`s use the palette, not the theme.** Set the palette's
   text roles to `theme.text()` on the central widget (checklist step 4),
   or labels can come out dark-on-dark on another machine's palette.
4. **Make every page scrollable.** A `QStackedWidget`'s minimum height is
   the **largest** of **all** its pages, hidden ones included. One tall
   unscrollable Settings page gave afterglow a 1128px minimum window
   height: the window couldn't be resized vertically, and fullscreen cut
   off the bottom. For any "window won't shrink / too big" report,
   check `minimumSizeHint()` down the widget tree first.
5. **`QScrollArea.setWidget()` turns `autoFillBackground` on** for the
   page widget. Turn it back off (on the page and the viewport) or the
   page paints over the intended background.
6. **`CustomGroupBox`: use `make_layout()`.** PySide6 doesn't reliably
   call a Python `setLayout` override when a layout is built as `QVBoxLayout(group)`,
   so the title's reserved margin silently never applied.
7. **`OutlinedLabel`: two passes.** A single stroke+fill `drawPath` lets
   the outline eat into the letters from both sides; at small sizes the
   fill vanished completely. Stroke first, then fill on top. Also, a
   thick outline at a small font size can still hide the fill; check
   actual sizes.
8. **`QPen` trap:** after `painter.setPen(Qt.NoPen)`, `painter.pen()`
   returns a pen whose *style* is NoPen, and setting a color/width on it
   still draws nothing. Build a fresh `QPen(color)` instead.
9. **Don't block the event loop with big rebuilds.** Rebuilding a list
   of many custom widgets synchronously froze afterglow, and a
   "debounce" couldn't help because spam clicks were queued until the
   rebuild ended. Afterglow now builds in ~12ms slices across event-loop
   ticks, coalesces extra requests into one follow-up rebuild, and swaps
   new widgets in all at once. Use the same approach for any long list
   (e.g. Puppetry macro lists, Conduit device lists).
10. **Cache pixmap work.** Decoding, scaling or rounding the same image
    every rebuild or repaint was a large share of afterglow's cost. Use
    `scaled_cached`, cache rendered thumbnails by path + mtime, and load
    bundled assets through `resource_qpixmap` (cached).
11. **Stale saved config looks like a rendering bug.** When a default
    color changes in code, an already-saved config keeps the old value.
    Several afterglow debugging rounds on "colors aren't applying" turned
    out to be exactly this. That's why the editor has "Revert to Default Colors"
    and "Revert to Default Settings". When a default is changed
    deliberately, add a targeted migration that only replaces the exact
    old default value, never a custom one.
12. **Offscreen tests aren't proof it looks right.** The sandbox has no
    real window manager, compositor or GPU. Layout, stylesheet cascade
    and WM behavior can differ on a real desktop. Verify real outcomes
    (measure widget sizes, sample pixels), and still request a visual
    check on a real display. Test pitfalls that caused false alarms in
    afterglow:
    - sampling the literal corner pixel of a rounded shape;
    - sampling on top of centered text;
    - windows under the offscreen cursor are drawn **hovered** (lighter
      fill, pulse-shrunk). Call `QCursor.setPos(4000, 4000)` first;
    - `grab()` on a top-level window is opaque, so alpha checks can't
      detect rounding. Compare colors instead;
    - sampling the center of a button samples its label, not its fill;
    - measuring a curve on the "first filled row" mid-animation lands a
      fraction of a pixel inside the shape, where a round corner is
      naturally narrower. Assert the real property instead (e.g. "the
      drawn scale never exceeds 1.0 on any frame");
    - checking that a signal fired or a value was set doesn't prove the
      layout is right. Afterglow once passed tests with its video
      squeezed to 20% of the window.
13. **A widget can't draw outside its own rectangle.** Any effect that
    grows a shape past the widget's edge (a bounce overshoot, a glow, a
    shadow) gets clipped silently. Rounded corners are the first thing
    to go. Keep the shape inside the rect, reserve headroom, or draw the
    effect on a parent/overlay widget instead.

14. **Clicks fire on mouse-UP, so click work lands on the release
    animation.** Qt emits `clicked` when the button is released, the same
    moment the release bounce starts. Hover and press stay smooth because
    nothing else runs then; release stutters if the click does real work.
    In afterglow, the sidebar Library button and Refresh rebuilt all 80
    library cards on every click, even when nothing had changed, and the
    release bounce froze for 70-80ms at a time (press was ~12ms/frame).
    The fix was to skip work that would produce an identical result:
    compare a cheap signature of what would be shown (row data, file
    mtimes, relevant settings) against what is already on screen, and do
    nothing when they match. That cut the worst release hitch to ~25ms
    and made 60 spam clicks cause zero rebuilds. Rules:
    - make click handlers cheap in the common case; detect "nothing
      changed" and return early;
    - time-slice and coalesce whatever genuinely has to run (pitfall 9);
    - measure frame gaps on press AND release per button, with the real
      click action connected, not just on a bare button. A bare button
      always looks smooth;
    - a cheaper-looking fix is not automatically better. Replacing the
      page-switch fade (a `QGraphicsOpacityEffect` on the whole page)
      with a one-time snapshot halved total work but concentrated it into
      one block at the start of the bounce, and an interleaved A/B showed
      a worse worst-hitch (42.7ms vs 35.7ms median). It was reverted.
      Judge by the worst frame gap, not total work.

---

## 8. What NOT to copy from afterglow

These are afterglow-specific and not part of the kit: video cards and
their nested boxes, gradient-image borders and the per-pixel hue-shift
(`pixmap_effects.py`), filter-icon outlines, the Library grid, the
trim timeline and volume bar, the video previewer overlay. Build each
app's own domain widgets from the kit's primitives instead.

---

## 9. Verification

`test_kit.py` (next to this guide) exercises the kit standalone:
placeholder colors, a custom per-app palette through the provider,
system-palette fallback, rounding on/off, corner skipping, pulse timing
on every pulsing class, theme editor apply/revert/invalid-color
handling, the unscoped-stylesheet check, and a frame-by-frame check that
the release bounce never crosses the widget edge (plus the headroom and
flush-edge behavior if the overshoot is raised). All 26 checks pass under
`QT_QPA_PLATFORM=offscreen`. Run it after copying the kit, and extend it
for app-specific widgets.

```
QT_QPA_PLATFORM=offscreen python3 test_kit.py
python3 demo_app.py          # to look at it on a real display
```
