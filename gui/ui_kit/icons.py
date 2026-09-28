"""
Icon loader. The ART comes from mrtw -- this only finds, recolors and
caches it, and every caller falls back to text when an icon isn't there
yet, so nothing looks broken while icons arrive one at a time.

Drop files into  gui/ui_kit/resources/icons/  named exactly:

  Sidebar (shown left of the label):
    nav_macros.svg        nav_editor.svg        nav_visualizer.svg
    nav_settings.svg

  Block palette section headers:
    block_output.svg      block_timing.svg      block_conditions.svg
    block_input.svg       block_variables.svg   block_functions.svg
    block_custom.svg      block_other.svg

  Optional, per block (shown at the left of that block in the palette):
    primitive_<name>.svg  e.g. primitive_tap.svg, primitive_move_mouse.svg,
                          primitive_waitForPress.svg

Format: SVG preferred (any size; drawn square). Draw it in ONE color using
fill="currentColor" / stroke="currentColor" (or plain #000000 / black) and
Puppetry recolors it to match where it's shown -- orange for input, blue
for output, the text color in the sidebar -- and follows the theme. A PNG
also works (<name>.png) but is shown as drawn, never recolored.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

ICON_DIR = Path(__file__).resolve().parent / "resources" / "icons"

EXPECTED = (
    "nav_macros", "nav_editor", "nav_visualizer", "nav_settings",
    "block_output", "block_timing", "block_conditions", "block_input", "block_variables", "block_functions",
    "block_custom", "block_other",
)

_cache: dict = {}


def icon_path(name: str) -> "Path | None":
    for ext in (".svg", ".png"):
        p = ICON_DIR / f"{name}{ext}"
        if p.is_file():
            return p
    return None


def _recolor_svg(src: str, color: QColor) -> str:
    hexv = color.name()
    src = src.replace("currentColor", hexv)
    return re.sub(r'(fill|stroke)\s*=\s*"(#000000|#000|black)"', rf'\1="{hexv}"', src)


def icon_pixmap(name: str, size: int = 24, color: "QColor | None" = None) -> "QPixmap | None":
    """The icon rendered at `size` px (x2 for HiDPI), recolored to `color`
    if it's an SVG -- or None if the file doesn't exist (callers fall back
    to text)."""
    p = icon_path(name)
    if p is None:
        return None
    key = (name, size, color.name() if color is not None else None, p.stat().st_mtime_ns)
    pm = _cache.get(key)
    if pm is not None:
        return pm
    ratio = 2.0
    if p.suffix == ".svg":
        try:
            from PySide6.QtSvg import QSvgRenderer
        except ImportError:
            return None
        src = p.read_text(errors="replace")
        if color is not None:
            src = _recolor_svg(src, color)
        renderer = QSvgRenderer(QByteArray(src.encode()))
        if not renderer.isValid():
            return None
        pm = QPixmap(int(size * ratio), int(size * ratio))
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
        painter.end()
    else:
        pm = QPixmap(str(p))
        if pm.isNull():
            return None
        pm = pm.scaled(int(size * ratio), int(size * ratio), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(ratio)
    _cache[key] = pm
    return pm


def missing() -> list[str]:
    """Expected icons that haven't been drawn yet."""
    return [n for n in EXPECTED if icon_path(n) is None]
