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

Each look also changes structure, not just paint: where the navigation
sits and how its tabs are drawn (nav_position / nav_style -> NavTab
below), what surrounds the pages (frame -> LookFrame) and how macro
blocks are drawn (block_style -> block_render.paint_block). Marionette is
the default; Classic keeps the original look.
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
    dark: dict[str, Any] = field(default_factory=dict)   # light looks: what their dark version changes

    @property
    def has_dark(self) -> bool:
        return bool(self.dark)

    def settings(self, base: ThemeSettings | None = None, dark: bool = False) -> ThemeSettings:
        """The look as ThemeSettings (its dark version with dark=True, when it
        has one). `base` supplies the switches a look doesn't own (custom
        widgets on/off, system palette on/off)."""
        start = asdict(base) if base is not None else asdict(ThemeSettings())
        fresh = asdict(ThemeSettings())
        # everything a look can define goes back to the kit default first,
        # so switching looks never leaves a value behind from the last one
        for k, v in fresh.items():
            if k not in ("custom_widgets_enabled", "app_theme_enabled"):
                start[k] = v
        start.update(self.values)
        use_dark = dark and self.has_dark
        if use_dark:
            start.update(self.dark)
        start["preset"] = self.key
        start["dark_variant"] = use_dark
        return ThemeSettings(**start)


LOOKS: dict[str, Look] = {}

DEFAULT_LOOK = "marionette"
# looks that existed once and were dropped: whoever had one gets the default
RETIRED_LOOKS = ("brutalist", "patchbay", "blackout")


def _look(key: str, name: str, blurb: str, **values: Any) -> None:
    LOOKS[key] = Look(key, name, blurb, values)


def _dark(key: str, **values: Any) -> None:
    """The dark version of a light look: the same structure and fonts, new colors."""
    LOOKS[key].dark.update(values)


_look("marionette", "Marionette",
      "A theatre: a velvet valance over the stage, a playbill for navigation, footlights below. "
      "Blocks hang on brass-edged cards.",
      color_app_background="#140b0c", color_page_background="#1a0f10", color_surface="#2a1719",
      color_accent="#b8904a", color_highlight="#3a2022", color_text="#f0e3cf", color_text_outline="#0c0607",
      color_general="#8c2f39", color_input="#d9a441", color_output="#5f8fb8", color_neutral_block="#8c7f72",
      color_custom="#a0527a", color_function="#c2607a", color_true="#6fa56a", color_false="#d0473d",
      rounded_corner_radius=6, ui_padding=16, border_width=1.5,
      font_body="Manrope", font_display="Cormorant Garamond", font_mono="JetBrains Mono",
      font_size=10, font_weight=500,
      nav_position="left", nav_style="playbill", frame="stage", block_style="strings")

_look("shadow_play", "Shadow play",
      "A lit cloth screen: pages glow warm, navigation is a row of silhouette puppets on rods along "
      "the bottom, blocks are black cut-outs. Light.",
      color_app_background="#c99a52", color_page_background="#f3dba6", color_surface="#f8e9c4",
      color_accent="#2a1c10", color_highlight="#d3a85c", color_text="#1c120a", color_text_outline="#f3dba6",
      color_general="#2a1c10", color_input="#b3541e", color_output="#1f5f8b", color_neutral_block="#6b5a45",
      color_custom="#7a3f8f", color_function="#a8326b", color_true="#3f7d3a", color_false="#b3261e",
      rounded_corner_radius=14, ui_padding=16, border_width=2.5,
      font_body="Manrope", font_display="Cinzel", font_mono="JetBrains Mono",
      font_size=10, font_weight=560,
      nav_position="bottom", nav_style="rod", frame="lightbox", block_style="silhouette")

_look("workbench", "Workbench",
      "A notebook on a wooden bench: ruled paper pages, colored index tabs down the right edge, "
      "blocks as white index cards with a colored strip. Light.",
      color_app_background="#8a6a48", color_page_background="#fbf6ea", color_surface="#ffffff",
      color_accent="#6e675b", color_highlight="#e9e1cf", color_text="#2b2823", color_text_outline="#efe7d6",
      color_general="#4f6d7a", color_input="#c46a1b", color_output="#2f6fb0", color_neutral_block="#7d776b",
      color_custom="#7b5ea7", color_function="#b0508a", color_true="#4c8c4a", color_false="#c0443a",
      rounded_corner_radius=3, ui_padding=14, border_width=1.5,
      font_body="Manrope", font_display="IBM Plex Sans Condensed", font_mono="IBM Plex Mono",
      font_size=10, font_weight=500, display_upper=True, pattern="ruled",
      nav_position="right", nav_style="index", frame="desk", block_style="cards")

_look("console", "Console",
      "A terminal: an amber status line on top, function-key navigation along the bottom, "
      "blocks drawn as outlines. Monospace everywhere.",
      color_app_background="#050505", color_page_background="#050505", color_surface="#0e0e0e",
      color_accent="#5a5a5a", color_highlight="#1a1a1a", color_text="#d8d8d8", color_text_outline="#000000",
      color_general="#8a5f00", color_input="#ffb000", color_output="#4fc3f7", color_neutral_block="#8a8a8a",
      color_custom="#c792ea", color_function="#f78fb3", color_true="#7ddc6b", color_false="#ff5c5c",
      rounded_corner_radius=0, ui_padding=12, border_width=1.0,
      font_body="JetBrains Mono", font_display="JetBrains Mono", font_mono="JetBrains Mono",
      font_size=10, font_weight=460, display_upper=True,
      nav_position="bottom", nav_style="fkeys", frame="terminal", block_style="outline")

_look("blueprint", "Blueprint",
      "A technical drawing: a blue grid inside a double border, sheet tabs along the top, "
      "blocks drawn as line-work components.",
      color_app_background="#0f3260", color_page_background="#123a6b", color_surface="#163f73",
      color_accent="#e8f1ff", color_highlight="#1d4a82", color_text="#ffffff", color_text_outline="#0b2649",
      color_general="#2f6cb5", color_input="#ffd27a", color_output="#7fe0ff", color_neutral_block="#c4d6ee",
      color_custom="#d0b3ff", color_function="#ffb3d9", color_true="#8ff0a4", color_false="#ff9c9c",
      rounded_corner_radius=0, ui_padding=16, border_width=1.5,
      font_body="IBM Plex Mono", font_display="IBM Plex Sans Condensed", font_mono="IBM Plex Mono",
      font_size=10, font_weight=500, display_upper=True, pattern="grid",
      nav_position="top", nav_style="sheets", frame="drawing", block_style="schematic")

_look("arcade", "Arcade",
      "A cabinet screen: scanlines, pixel type, L1/R1-style navigation across the top, "
      "chunky blocks with hard drop shadows.",
      color_app_background="#140a24", color_page_background="#1e1036", color_surface="#2e1a52",
      color_accent="#ff4fd8", color_highlight="#3b2466", color_text="#f4ecff", color_text_outline="#140a24",
      color_general="#b12fa0", color_input="#ff9f1c", color_output="#4df3ff", color_neutral_block="#c9b6e8",
      color_custom="#b78cff", color_function="#ff7ac8", color_true="#6dff8a", color_false="#ff4d6d",
      rounded_corner_radius=0, ui_padding=16, border_width=3.0,
      font_body="VT323", font_display="Press Start 2P", font_mono="VT323",
      font_size=15, display_upper=True, pattern="scanlines",
      nav_position="top", nav_style="arcade", frame="cabinet", block_style="pixel")

_look("felt", "Felt",
      "Hand puppets: green felt, big soft corners, sewn tabs along the bottom, "
      "blocks with stitching around the edge.",
      color_app_background="#1c3626", color_page_background="#23422f", color_surface="#2e553d",
      color_accent="#5c9470", color_highlight="#3a6a4c", color_text="#f6f1e3", color_text_outline="#12241a",
      color_general="#c9578c", color_input="#f29a3f", color_output="#58a8e8", color_neutral_block="#a9b8a4",
      color_custom="#b58be0", color_function="#ee82b4", color_true="#9bd65b", color_false="#e8554a",
      rounded_corner_radius=26, ui_padding=16, border_width=3.0,
      font_body="Sora", font_display="Luckiest Guy", font_mono="JetBrains Mono",
      font_size=10, font_weight=460, pattern="weave",
      nav_position="bottom", nav_style="sewn", frame="felt", block_style="stitched")

_look("scrapbook", "Scrapbook",
      "A craft book on a wooden table (LittleBigPlanet-inspired): taped paper tabs along the top, "
      "blocks cut from cardboard, wood, felt, sponge and glossy plastic. Light.",
      color_app_background="#5b3a24", color_page_background="#fbf3e2", color_surface="#ffffff",
      color_accent="#d9c8a6", color_highlight="#f1e3c6", color_text="#2b1d12", color_text_outline="#fbf3e2",
      color_general="#e8823a", color_input="#c88a4a", color_output="#2f7fc0", color_neutral_block="#8b9a86",
      color_custom="#9b6fc4", color_function="#d0607f", color_true="#5fae3c", color_false="#d0473d",
      rounded_corner_radius=12, ui_padding=16, border_width=2.0,
      font_body="Nunito", font_display="Fredoka", font_mono="JetBrains Mono",
      font_size=10, font_weight=600,
      nav_position="top", nav_style="taped", frame="table", block_style="materials")

# ---- dark versions of the light looks ----------------------------------------

# a night show: the screen is dark and the cut-outs are lit, cream on brown
_dark("shadow_play",
      color_app_background="#0d0906", color_page_background="#1f1610", color_surface="#2a1e16",
      color_accent="#f3dba6", color_highlight="#3a2a1c", color_text="#f3dba6", color_text_outline="#0d0906",
      color_general="#f3dba6", color_input="#e2894a", color_output="#5fa8dc", color_neutral_block="#a8957a",
      color_custom="#b37cc9", color_function="#d66a9c", color_true="#6fb865", color_false="#e0574d")

# the same bench after hours: dark wood, charcoal paper, chalky lines
_dark("workbench",
      color_app_background="#3a2a1c", color_page_background="#1f1d1a", color_surface="#2a2724",
      color_accent="#8a8274", color_highlight="#34302b", color_text="#ece6d8", color_text_outline="#1f1d1a",
      color_general="#7fa3b2", color_input="#e08a3c", color_output="#5a9be0", color_neutral_block="#a39d90",
      color_custom="#a68ad0", color_function="#d278ac", color_true="#6fb06d", color_false="#e0645a")

# a black-paper scrapbook on a dark table: the colored tabs and materials stay bright
_dark("scrapbook",
      color_app_background="#24160d", color_page_background="#26201c", color_surface="#352d27",
      color_accent="#6b5a45", color_highlight="#3d332c", color_text="#f6ecd8", color_text_outline="#26201c",
      color_general="#f0954f", color_input="#d89a58", color_output="#4a9be0", color_neutral_block="#9bab95",
      color_custom="#b48ad8", color_function="#e07a96", color_true="#72c24f", color_false="#e65a4f")


_look("classic", "Classic", "The original grey look: desktop fonts, a sidebar, plain puzzle blocks.")


def look_for(settings: ThemeSettings) -> Look:
    return LOOKS.get(getattr(settings, "preset", "") or "", LOOKS["classic"])


LOOK_VERSION = 2


def ensure_default_look(cfg) -> None:
    """Once per install: anyone who never picked a look (or had one that was
    dropped) gets the default one. Runs before the window is built."""
    try:
        state = cfg.load_state()
    except Exception:
        return
    if int(state.get("look_version", 0) or 0) >= LOOK_VERSION:
        return
    current = (state.get("theme") or {}).get("preset", "")
    if current in ("", "classic") + RETIRED_LOOKS:
        state["theme"] = asdict(LOOKS[DEFAULT_LOOK].settings())
    state["look_version"] = LOOK_VERSION
    try:
        cfg.save_state(state)
    except Exception:
        pass


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

_tile_cache: dict = {}


def pattern_tile(pattern: str, base_rgb: int, text_rgb: int, margin_rgb: int = 0):
    """One repeat of a page pattern as a QPixmap (cached): pages fill with it
    as a brush instead of drawing hundreds of lines or dots each paint."""
    key = (pattern, base_rgb, text_rgb, margin_rgb)
    tile = _tile_cache.get(key)
    if tile is not None:
        return tile
    from PySide6.QtCore import QLineF, QPointF
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
    base, text = QColor.fromRgb(base_rgb), QColor.fromRgb(text_rgb)

    def mix(amount: float) -> QColor:
        return QColor(int(base.red() + (text.red() - base.red()) * amount),
                      int(base.green() + (text.green() - base.green()) * amount),
                      int(base.blue() + (text.blue() - base.blue()) * amount))

    size = {"grid": (120, 120), "dots": (22, 22), "scanlines": (4, 4), "ruled": (8, 28), "weave": (6, 6)}.get(pattern, (8, 8))
    tile = QPixmap(*size)
    tile.fill(base)
    p = QPainter(tile)
    if pattern == "grid":
        p.setPen(QPen(mix(0.07)))
        for i in range(1, 5):
            p.drawLine(QLineF(i * 24, 0, i * 24, 120))
            p.drawLine(QLineF(0, i * 24, 120, i * 24))
        p.setPen(QPen(mix(0.14)))
        p.drawLine(QLineF(0, 0, 0, 120))
        p.drawLine(QLineF(0, 0, 120, 0))
    elif pattern == "dots":
        pen = QPen(mix(0.16))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawPoint(QPointF(11, 11))
    elif pattern == "scanlines":
        p.setPen(QPen(mix(0.08)))
        p.drawLine(QLineF(0, 0, 4, 0))
    elif pattern == "ruled":
        p.setPen(QPen(mix(0.10)))
        p.drawLine(QLineF(0, 27, 8, 27))
    elif pattern == "weave":
        p.setPen(QPen(mix(0.05)))
        p.drawLine(QLineF(0, 0, 0, 6))
        p.setPen(QPen(mix(0.035)))
        p.drawLine(QLineF(0, 0, 6, 0))
    p.end()
    if len(_tile_cache) > 64:
        _tile_cache.clear()
    _tile_cache[key] = tile
    return tile


def paint_pattern(painter, rect, settings: ThemeSettings, base) -> None:
    """Draws the look's page pattern over an already-filled page, as a tiled
    brush (pattern_tile). Lines are the page color nudged toward the text
    color, so a pattern is texture, never contrast."""
    from PySide6.QtCore import QLineF
    from PySide6.QtGui import QBrush, QColor, QPen, QTransform
    pattern = getattr(settings, "pattern", "")
    if not pattern:
        return
    tile = pattern_tile(pattern, base.rgb(), QColor(settings.color_text).rgb())
    brush = QBrush(tile)
    brush.setTransform(QTransform.fromTranslate(rect.left(), rect.top()))
    painter.fillRect(rect, brush)
    if pattern == "ruled":                 # the red margin line of ruled paper
        margin = QColor(settings.color_false)
        margin.setAlpha(70)
        painter.setPen(QPen(margin))
        painter.drawLine(QLineF(rect.left() + 44, rect.top(), rect.left() + 44, rect.bottom()))
