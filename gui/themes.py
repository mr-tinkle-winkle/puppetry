"""Looks: whole-app themes picked by name in Settings > Theme.

A look is a full set of ThemeSettings values -- every color role, corner
radius, padding, outline weight, the three font roles and the page pattern
-- written into state.json's "theme" when it is applied. The Appearance
editor below the picker still adjusts individual values afterwards.

The meaning of the semantic colors never changes between looks: input
(reads the real world) stays in the orange family, output (acts on it) in
the blue family, true/enabled green, false/disabled red. A look only moves
them to fit its palette.

Fonts are bundled (gui/resources/fonts and the overlay's own
gui/overlay_web/fonts, all SIL Open Font License), so a look reads the same
on any desktop.

These ten are CONCEPTS: colors, type, shape and pattern. Tab styles and
motion (as in Jeeves' looks) are not part of them yet.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ui_kit.theme_config import ThemeSettings

FONT_DIR = Path(__file__).resolve().parent / "resources" / "fonts"

PATTERNS = ("", "grid", "dots", "scanlines", "ruled", "weave")


@dataclass(frozen=True)
class Look:
    key: str
    name: str
    blurb: str
    values: dict[str, Any] = field(default_factory=dict)

    def settings(self, base: ThemeSettings | None = None) -> ThemeSettings:
        """The look as ThemeSettings. `base` supplies the switches a look
        doesn't own (custom widgets on/off, system palette on/off)."""
        start = asdict(base) if base is not None else asdict(ThemeSettings())
        fresh = asdict(ThemeSettings())
        # everything a look can define goes back to the kit default first,
        # so switching looks never leaves a value behind from the last one
        for k, v in fresh.items():
            if k not in ("custom_widgets_enabled", "app_theme_enabled"):
                start[k] = v
        start.update(self.values)
        start["preset"] = self.key
        return ThemeSettings(**start)


LOOKS: dict[str, Look] = {}


def _look(key: str, name: str, blurb: str, **values: Any) -> None:
    LOOKS[key] = Look(key, name, blurb, values)


_look("classic", "Classic", "The original grey look, desktop fonts.")

_look("marionette", "Marionette",
      "The house look: a velvet curtain, brass fittings and a serif. Strings are thin.",
      color_app_background="#1a0f10", color_page_background="#140b0c", color_surface="#2a1719",
      color_accent="#7a5a34", color_highlight="#3a2022", color_text="#f0e3cf", color_text_outline="#0c0607",
      color_general="#8c2f39", color_input="#d9a441", color_output="#5f8fb8", color_neutral_block="#8c7f72",
      color_custom="#a0527a", color_function="#c2607a", color_true="#6fa56a", color_false="#d0473d",
      rounded_corner_radius=6, ui_padding=16, border_width=1.5,
      font_body="Manrope", font_display="Cormorant Garamond", font_mono="JetBrains Mono",
      font_size=10, font_weight=500)

_look("shadow_play", "Shadow play",
      "A lit cloth screen and black silhouettes, after wayang shadow puppets. Light.",
      color_app_background="#e2bd78", color_page_background="#f3dba6", color_surface="#f8e9c4",
      color_accent="#2a1c10", color_highlight="#d3a85c", color_text="#1c120a", color_text_outline="#f3dba6",
      color_general="#2a1c10", color_input="#b3541e", color_output="#1f5f8b", color_neutral_block="#6b5a45",
      color_custom="#7a3f8f", color_function="#a8326b", color_true="#3f7d3a", color_false="#b3261e",
      rounded_corner_radius=14, ui_padding=16, border_width=2.5,
      font_body="Manrope", font_display="Cinzel", font_mono="JetBrains Mono",
      font_size=10, font_weight=560, pattern="weave")

_look("workbench", "Workbench",
      "The puppet maker's bench: paper, graphite and ruled lines. Light.",
      color_app_background="#d9cfbd", color_page_background="#efe7d6", color_surface="#fbf6ea",
      color_accent="#6e675b", color_highlight="#cfc4ae", color_text="#2b2823", color_text_outline="#efe7d6",
      color_general="#4f6d7a", color_input="#c46a1b", color_output="#2f6fb0", color_neutral_block="#7d776b",
      color_custom="#7b5ea7", color_function="#b0508a", color_true="#4c8c4a", color_false="#c0443a",
      rounded_corner_radius=3, ui_padding=14, border_width=1.5,
      font_body="Manrope", font_display="IBM Plex Sans Condensed", font_mono="IBM Plex Mono",
      font_size=10, font_weight=500, display_upper=True, pattern="ruled")

_look("console", "Console",
      "A terminal: monospace everywhere, square, black with amber.",
      color_app_background="#050505", color_page_background="#050505", color_surface="#0e0e0e",
      color_accent="#3a3a3a", color_highlight="#1a1a1a", color_text="#d8d8d8", color_text_outline="#000000",
      color_general="#8a5f00", color_input="#ffb000", color_output="#4fc3f7", color_neutral_block="#8a8a8a",
      color_custom="#c792ea", color_function="#f78fb3", color_true="#7ddc6b", color_false="#ff5c5c",
      rounded_corner_radius=0, ui_padding=12, border_width=1.0,
      font_body="JetBrains Mono", font_display="JetBrains Mono", font_mono="JetBrains Mono",
      font_size=10, font_weight=460, display_upper=True)

_look("blueprint", "Blueprint",
      "White line work on a blue grid, like the drawing of a mechanism.",
      color_app_background="#0f3260", color_page_background="#123a6b", color_surface="#163f73",
      color_accent="#e8f1ff", color_highlight="#1d4a82", color_text="#ffffff", color_text_outline="#0b2649",
      color_general="#2f6cb5", color_input="#ffd27a", color_output="#7fe0ff", color_neutral_block="#c4d6ee",
      color_custom="#d0b3ff", color_function="#ffb3d9", color_true="#8ff0a4", color_false="#ff9c9c",
      rounded_corner_radius=0, ui_padding=16, border_width=1.5,
      font_body="IBM Plex Mono", font_display="IBM Plex Sans Condensed", font_mono="IBM Plex Mono",
      font_size=10, font_weight=500, display_upper=True, pattern="grid")

_look("arcade", "Arcade",
      "Pixel type, scanlines, pink and cyan. Loud on purpose.",
      color_app_background="#140a24", color_page_background="#1e1036", color_surface="#2e1a52",
      color_accent="#ff4fd8", color_highlight="#3b2466", color_text="#f4ecff", color_text_outline="#140a24",
      color_general="#b12fa0", color_input="#ff9f1c", color_output="#4df3ff", color_neutral_block="#c9b6e8",
      color_custom="#b78cff", color_function="#ff7ac8", color_true="#6dff8a", color_false="#ff4d6d",
      rounded_corner_radius=0, ui_padding=16, border_width=3.0,
      font_body="VT323", font_display="Press Start 2P", font_mono="VT323",
      font_size=15, display_upper=True, pattern="scanlines")

_look("brutalist", "Brutalist",
      "Black on white, heavy outlines, capitals. Light.",
      color_app_background="#ffffff", color_page_background="#ffffff", color_surface="#ffffff",
      color_accent="#000000", color_highlight="#e8e8e8", color_text="#000000", color_text_outline="#ffffff",
      color_general="#000000", color_input="#ff5a00", color_output="#0038ff", color_neutral_block="#3d3d3d",
      color_custom="#7a00ff", color_function="#ff0080", color_true="#00a23c", color_false="#e00000",
      rounded_corner_radius=0, ui_padding=14, border_width=4.0,
      font_body="Archivo", font_display="Archivo Black", font_mono="JetBrains Mono",
      font_size=10, font_weight=500, display_upper=True)

_look("patchbay", "Patchbay",
      "A rack of jacks and cables: charcoal, with input and output as the two cable colors.",
      color_app_background="#17181b", color_page_background="#101114", color_surface="#22242a",
      color_accent="#3b3f4a", color_highlight="#2b2e36", color_text="#e9eaee", color_text_outline="#08090a",
      color_general="#5b6274", color_input="#ff8a3d", color_output="#3da5ff", color_neutral_block="#9aa1b2",
      color_custom="#b07cff", color_function="#ff6fb1", color_true="#3ddc84", color_false="#ff4d4d",
      rounded_corner_radius=18, ui_padding=14, border_width=2.0,
      font_body="Space Grotesk", font_display="Space Grotesk", font_mono="JetBrains Mono",
      font_size=10, font_weight=480, pattern="dots")

_look("felt", "Felt",
      "Hand puppets: green felt, big soft corners, friendly type.",
      color_app_background="#23422f", color_page_background="#1c3626", color_surface="#2e553d",
      color_accent="#5c9470", color_highlight="#3a6a4c", color_text="#f6f1e3", color_text_outline="#12241a",
      color_general="#c9578c", color_input="#f29a3f", color_output="#58a8e8", color_neutral_block="#a9b8a4",
      color_custom="#b58be0", color_function="#ee82b4", color_true="#9bd65b", color_false="#e8554a",
      rounded_corner_radius=26, ui_padding=16, border_width=3.0,
      font_body="Sora", font_display="Luckiest Guy", font_mono="JetBrains Mono",
      font_size=10, font_weight=460, pattern="weave")

_look("blackout", "Blackout",
      "House lights off: true black, hairlines, nothing decorative.",
      color_app_background="#000000", color_page_background="#000000", color_surface="#0b0b0c",
      color_accent="#2a2a2e", color_highlight="#141416", color_text="#f2f2f3", color_text_outline="#000000",
      color_general="#26303a", color_input="#ff9a57", color_output="#63c7ff", color_neutral_block="#7c7c84",
      color_custom="#a98bff", color_function="#ff7fb5", color_true="#5fe08a", color_false="#ff5f6d",
      rounded_corner_radius=10, ui_padding=14, border_width=1.0,
      font_body="Manrope", font_display="Manrope", font_mono="JetBrains Mono",
      font_size=10, font_weight=480)


def look_for(settings: ThemeSettings) -> Look:
    return LOOKS.get(getattr(settings, "preset", "classic") or "classic", LOOKS["classic"])


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------

_loaded = False


def load_fonts() -> None:
    """Every bundled face, once per process (needs a QGuiApplication)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from PySide6.QtGui import QFontDatabase
    import font_catalog
    font_catalog.register_qt_fonts()          # the overlay's set (Cinzel, Press Start 2P, Luckiest Guy, ...)
    for f in sorted(FONT_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(f))


_system_font = None


def apply_fonts(app, settings: ThemeSettings) -> None:
    """The look's body font as the application font, and its mono font
    wherever the code asks for "monospace"."""
    from PySide6.QtGui import QFont
    global _system_font
    load_fonts()
    if _system_font is None:
        _system_font = QFont(app.font())
    f = QFont(_system_font)
    if settings.font_body:
        f.setFamily(settings.font_body)
    if settings.font_size:
        f.setPointSize(int(settings.font_size))
    if settings.font_weight:
        f.setWeight(QFont.Weight(int(settings.font_weight)))
    app.setFont(f)
    QFont.removeSubstitutions("monospace")
    if settings.font_mono:
        QFont.insertSubstitution("monospace", settings.font_mono)


def display_font(settings: ThemeSettings, pixel_size: int = 15):
    """Section titles: the look's display face (capitals if it says so)."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    f = QFont(QApplication.font())
    family = settings.font_display or settings.font_body
    if family:
        f.setFamily(family)
    # pixel faces are far wider than their size suggests
    f.setPixelSize(max(8, pixel_size - 6) if family == "Press Start 2P" else pixel_size)
    f.setBold(family not in ("Press Start 2P", "Archivo Black", "Luckiest Guy", "Cinzel"))
    if settings.display_upper:
        f.setCapitalization(QFont.AllUppercase)
        f.setLetterSpacing(QFont.PercentageSpacing, 104)
    return f


# ---------------------------------------------------------------------------
# page patterns
# ---------------------------------------------------------------------------

def paint_pattern(painter, rect, settings: ThemeSettings, base) -> None:
    """Draws the look's page pattern over an already-filled page. `base` is
    the page color: lines are that color nudged toward the text color, so
    a pattern is texture, never contrast."""
    from PySide6.QtCore import QPointF, QLineF
    from PySide6.QtGui import QColor, QPen
    pattern = getattr(settings, "pattern", "")
    if not pattern:
        return
    text = QColor(settings.color_text)

    def mix(amount: float) -> QColor:
        return QColor(int(base.red() + (text.red() - base.red()) * amount),
                      int(base.green() + (text.green() - base.green()) * amount),
                      int(base.blue() + (text.blue() - base.blue()) * amount))

    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing, False)
    if pattern == "grid":
        minor, major = QPen(mix(0.07)), QPen(mix(0.14))
        step = 24
        lines_minor, lines_major = [], []
        for i, x in enumerate(range(left, right + 1, step)):
            (lines_major if i % 5 == 0 else lines_minor).append(QLineF(x, top, x, bottom))
        for i, y in enumerate(range(top, bottom + 1, step)):
            (lines_major if i % 5 == 0 else lines_minor).append(QLineF(left, y, right, y))
        painter.setPen(minor)
        painter.drawLines(lines_minor)
        painter.setPen(major)
        painter.drawLines(lines_major)
    elif pattern == "dots":
        pen = QPen(mix(0.16))
        pen.setWidth(2)
        painter.setPen(pen)
        step = 22
        painter.drawPoints([QPointF(x, y) for y in range(top + step // 2, bottom, step)
                            for x in range(left + step // 2, right, step)])
    elif pattern == "scanlines":
        painter.setPen(QPen(mix(0.08)))
        painter.drawLines([QLineF(left, y, right, y) for y in range(top, bottom + 1, 4)])
    elif pattern == "ruled":
        painter.setPen(QPen(mix(0.10)))
        painter.drawLines([QLineF(left, y, right, y) for y in range(top + 28, bottom + 1, 28)])
        margin = QColor(settings.color_false)
        margin.setAlpha(70)
        painter.setPen(QPen(margin))
        painter.drawLine(QLineF(left + 44, top, left + 44, bottom))
    elif pattern == "weave":
        step = 6
        painter.setPen(QPen(mix(0.05)))
        painter.drawLines([QLineF(x, top, x, bottom) for x in range(left, right + 1, step)])
        painter.setPen(QPen(mix(0.035)))
        painter.drawLines([QLineF(left, y, right, y) for y in range(top, bottom + 1, step)])
    painter.restore()
