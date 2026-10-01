"""
The overlay's bundled fonts (overlay_web/fonts/, latin subsets as TTF,
from the Fontsource packages of Google Fonts; licenses in
overlay_web/fonts/licenses/ -- SIL OFL 1.1 or Apache 2.0).

One catalog, three users:
  * the font picker (overlay_settings.FontPicker) lists them by category,
    each drawn in its own font;
  * the Qt painters (app, previews, renderer) get them through
    register_qt_fonts();
  * the OBS pages load them from the overlay server: /fonts.css
    (font_face_css()) and /fonts/<file>.ttf.
No Qt import at module level (the server is stdlib only).
"""
from __future__ import annotations

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "overlay_web" / "fonts"

# (family, category, file stem, weights) -- in picker order, grouped by category
CATALOG = [
    ("sans-serif", "System", None, ()),
    ("serif", "System", None, ()),
    ("monospace", "System", None, ()),
    ("Comic Neue", "Comic", "comic-neue", (400, 700)),
    ("Bangers", "Comic", "bangers", (400,)),
    ("Luckiest Guy", "Comic", "luckiest-guy", (400,)),
    ("Patrick Hand", "Comic", "patrick-hand", (400,)),
    ("Permanent Marker", "Comic", "permanent-marker", (400,)),
    ("Tinos", "Serif", "tinos", (400, 700)),
    ("Playfair Display", "Serif", "playfair-display", (400, 700)),
    ("Cinzel", "Serif", "cinzel", (400, 700)),
    ("Roboto", "Sans", "roboto", (400, 700)),
    ("Montserrat", "Sans", "montserrat", (400, 700)),
    ("Oswald", "Sans", "oswald", (400, 700)),
    ("Anton", "Sans", "anton", (400,)),
    ("Bebas Neue", "Sans", "bebas-neue", (400,)),
    ("Courier Prime", "Typewriter", "courier-prime", (400, 700)),
    ("Special Elite", "Typewriter", "special-elite", (400,)),
    ("Pacifico", "Script", "pacifico", (400,)),
    ("Caveat", "Script", "caveat", (400, 700)),
    ("Lobster", "Script", "lobster", (400,)),
    ("Press Start 2P", "Novelty", "press-start-2p", (400,)),
    ("Creepster", "Novelty", "creepster", (400,)),
    ("Orbitron", "Novelty", "orbitron", (400, 700)),
]
GENERIC = {"sans-serif", "serif", "monospace", "cursive", "fantasy", "system-ui"}
SYSTEM_LABELS = {"sans-serif": "System sans", "serif": "System serif", "monospace": "System mono"}


def font_files() -> dict:
    """{file name: (family, weight)} for every bundled file that exists."""
    out = {}
    for family, _cat, stem, weights in CATALOG:
        for w in weights:
            name = f"{stem}-{w}.ttf"
            if (FONT_DIR / name).exists():
                out[name] = (family, w)
    return out


def css_stack(family: str) -> str:
    """A CSS font-family list for `family` (quoted, with a generic fallback)."""
    family = (family or "sans-serif").strip()
    if family in GENERIC:
        return family
    return '"' + family.replace('"', "") + '", sans-serif'


def font_face_css() -> str:
    rules = []
    for name, (family, w) in sorted(font_files().items()):
        rules.append(f'@font-face {{ font-family: "{family}"; font-weight: {w}; font-style: normal; '
                     f'font-display: block; src: url("/fonts/{name}") format("truetype"); }}')
    return "\n".join(rules) + "\n"


_registered = False


def register_qt_fonts() -> None:
    """Make the bundled fonts available to QFont (needs a Q(Gui)Application)."""
    global _registered
    if _registered:
        return
    from PySide6.QtGui import QFontDatabase
    for name in font_files():
        QFontDatabase.addApplicationFont(str(FONT_DIR / name))
    _registered = True
