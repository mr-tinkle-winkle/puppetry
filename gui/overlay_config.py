"""
overlay.json: everything about the OBS pages, the layered replay buffer
and how the keyboard/mouse overlay looks.

Stored at ~/.config/macro-daemon/overlay.json. The daemon only reads the
"enabled" flags (to decide whether to start puppetry-overlay); the helper
re-reads the file whenever it changes, so style edits show up live in OBS
without a restart.

STYLE_SCHEMA drives the settings form in the GUI, the defaults, and what
the web pages receive -- add an option here and it appears everywhere.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from puppetry_config import CONFIG_DIR  # noqa: E402  (one definition of the config dir)

# (key, label, type, default, extra) -- type: bool | color | int | float | choice | text
# extra: (min, max) for numbers, list of (value, label) for choice.
# Groups are only for laying out the form.
MOTION_OPTIONS = [
    ("trail_seconds", "Trail length (seconds)", "float", 1.0, (0.1, 10.0)),
    ("trail_width", "Trail thickness (% of the view)", "float", 4.0, (0.5, 20.0)),
    ("auto_zoom", "Mousepad: auto zoom (zoom out so the whole trail stays in view)", "bool", True, None),
    ("pad_fraction", "View covers this much of your screen height (%)", "int", 80, (5, 500)),
    ("screen_height", "Screen height (px)", "int", 1080, (240, 8640)),
    ("unzoom_s", "Mousepad: zoom back in after (seconds without zooming out)", "float", 0.6, (0.0, 30.0)),
    ("recenter_s", "Mousepad: re-center after resting (seconds)", "float", 1.0, (0.1, 30.0)),
    ("joystick_speed", "Joystick: speed for a full push (px/s)", "int", 3000, (100, 50000)),
]

STYLE_SCHEMA = [
    # (what's shown and where: the Input Visualizer's Edit mode -> "scene")
    ("Size", [
        ("unit", "Key size (px)", "int", 48, (16, 200)),
        ("gap", "Gap between keys (% of a key)", "int", 8, (0, 40)),
        ("radius", "Key corner rounding (% of a key)", "int", 14, (0, 50)),
        ("padding", "Padding around everything (px)", "int", 8, (0, 200)),
    ]),
    ("Colors", [
        ("background", "Background", "color", "#00000000", None),
        ("key_color", "Key", "color", "#2b2b2bdd", None),
        ("key_outline", "Key outline", "color", "#111111ff", None),
        ("outline_width", "Outline width (px)", "float", 1.0, (0.0, 10.0)),
        ("pressed_color", "Your input (input color)", "color", "#e0955aff", None),
        ("macro_color", "A macro's output (output color)", "color", "#5a9ee0ff", None),
        ("text_color", "Text", "color", "#e6e6e6ff", None),
        ("pressed_text_color", "Text on a pressed key", "color", "#111111ff", None),
        ("glow", "Glow around pressed keys", "bool", True, None),
        ("arrow_color", "Mouse movement (your input)", "color", "#e0955aff", None),
    ]),
    ("Text", [
        ("font_family", "Font", "font", "sans-serif", None),
        ("font_scale", "Text size (% of a key)", "int", 32, (10, 80)),
        ("bold", "Bold", "bool", False, None),
        ("label_mode", "Key labels", "choice", "label",
         [("label", "Short labels (A, Shift, F1)"), ("name", "Key names (KEY_A)"), ("none", "No labels")]),
        ("controller_labels", "Controller button labels", "choice", "xbox",
         [("xbox", "Xbox (A B X Y)"), ("playstation", "PlayStation (\u2715 \u25cb \u25a1 \u25b3)"),
          ("nintendo", "Nintendo (B A Y X)")]),
    ]),
    ("Behavior", [
        ("show_timers", "Show how long each key is held (replaces its label)", "bool", True, None),
        ("timer_delay_ms", "Start the timer after (ms)", "int", 0, (0, 5000)),
        ("release_fade_ms", "Fade out after release (ms)", "int", 120, (0, 3000)),
        ("show_macro_output", "Show what macros press and move (in the output color)", "bool", True, None),
        ("wheel_flash_ms", "Wheel flash (ms)", "int", 250, (0, 3000)),
    ]),
    ("Mouse movement (Comet, Mousepad, Joystick)", MOTION_OPTIONS),
]

SIMPLE_SCHEMA = [
    ("Simple input list", [
        ("font_family", "Font", "font", "sans-serif", None),
        ("font_px", "Text size (px)", "int", 36, (8, 300)),
        ("bold", "Bold", "bool", True, None),
        ("text_color", "Text", "color", "#ffffffff", None),
        ("outline_color", "Text outline", "color", "#000000ff", None),
        ("outline_px", "Outline width (px)", "int", 3, (0, 20)),
        ("background", "Background", "color", "#00000000", None),
        ("separator", "Separator", "text", " + ", None),
        ("color_by_source", "Color by source (your input / a macro's output)", "bool", True, None),
        ("real_color", "Your input", "color", "#e0955aff", None),
        ("macro_color", "A macro's output", "color", "#5a9ee0ff", None),
        ("show_macro_output", "Include what macros press", "bool", True, None),
        ("show_controller", "Include controller buttons", "bool", True, None),
        ("controller_labels", "Controller button names", "choice", "xbox",
         [("xbox", "Xbox"), ("playstation", "PlayStation"), ("nintendo", "Nintendo")]),
        ("names", "Names", "choice", "label", [("label", "Short (Ctrl, A, LMB)"), ("name", "Key names (KEY_A)")]),
        ("show_timers", "Show hold timers", "bool", False, None),
        ("show_mouse_buttons", "Include mouse buttons", "bool", True, None),
        ("show_wheel", "Include the wheel (briefly)", "bool", True, None),
        ("align", "Alignment", "choice", "left", [("left", "Left"), ("center", "Center"), ("right", "Right")]),
        ("history", "Keep recently released inputs for (ms)", "int", 0, (0, 10000)),
        ("empty_text", "Text when nothing is held", "text", "", None),
    ]),
]

MOVEMENT_SCHEMA = [
    ("Mouse movement page", [
        ("motion", "Style", "choice", "mousepad", [("mousepad", "Mousepad"), ("comet", "Comet"), ("joystick", "Joystick")]),
        ("center", "Comet follows", "choice", "head", [("head", "Follows head (the pointer stays centered)"),
                                                        ("tail", "Follows tail (the trail's end stays centered)")]),
        ("invert_side_rings", "Invert side button rings (back goes right, forward left)", "bool", False, None),
        ("arrow_color", "Your movement", "color", "#e0955aff", None),
        ("macro_color", "A macro's movement", "color", "#5a9ee0ff", None),
        ("show_macro_output", "Show movement made by macros", "bool", True, None),
        ("size", "Box size (px)", "int", 240, (40, 2000)),
        ("background", "Background", "color", "#00000000", None),
    ]),
    ("Behavior", MOTION_OPTIONS),
]


def schema_defaults(schema) -> dict:
    return {key: default for _g, opts in schema for key, _l, _t, default, _e in opts}


import kbm_layout as _kl  # noqa: E402

DEFAULTS = {
    # The input overlay: the scene (arranged in the Input Visualizer's Edit
    # mode) at http://127.0.0.1:<port>/, and -- with element_sources --
    # every element on its own at /el/<id> on the same port.
    "full": {"enabled": False, "port": 17380, "element_sources": False},
    "scene": copy.deepcopy(_kl.DEFAULT_SCENE),
    # mouse_movement / mouse_port: the arrow-only "movement" page
    "simple": {"enabled": False, "port": 17381, "mouse_movement": False, "mouse_port": 17382,
               "width": 900, "height": 120},
    "replay": {"enabled": False, "fallback_seconds": 60, "extra_seconds": 5},
    "obs": {"host": "127.0.0.1", "port": 4455, "password": ""},
    "style": schema_defaults(STYLE_SCHEMA),
    "simple_style": schema_defaults(SIMPLE_SCHEMA),
    "movement_style": schema_defaults(MOVEMENT_SCHEMA),
}
MOUSE_SCHEMA = MOVEMENT_SCHEMA            # old name


def config_file() -> Path:
    return CONFIG_DIR / "overlay.json"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    d = Path(base) / "puppetry" if base else CONFIG_DIR
    return d


def buffer_file() -> Path:
    return runtime_dir() / "input_buffer.jsonl"


def event_socket() -> Path:
    return runtime_dir() / "events.sock"


def status_file() -> Path:
    return runtime_dir() / "overlay_status.json"


DEFAULTS_REV = 2     # bump when a default changes in a way saved configs should pick up


def merged(data: dict) -> dict:
    data = copy.deepcopy(data or {})
    rev = int(data.pop("defaults_rev", 0) or 0)
    if rev < 1:     # Session 20: the comet follows its head by default (it used to default to the tail)
        for el in (data.get("scene") or {}).get("elements", []) if isinstance(data.get("scene"), dict) else []:
            if isinstance(el, dict) and el.get("type") == "comet" and el.get("center") == "tail":
                el["center"] = "head"
        ms = data.get("movement_style") or data.get("mouse_style")
        if isinstance(ms, dict) and ms.get("center") == "tail":
            ms["center"] = "head"
    if rev < 2:     # Session 21: Mousepad is the default movement view (the default comet becomes one)
        scene = data.get("scene") if isinstance(data.get("scene"), dict) else {}
        for el in scene.get("elements", []):
            if isinstance(el, dict) and el.get("id") == "comet" and el.get("type") == "comet":
                el["type"] = "mousepad"
                if not any(e.get("id") == "movement" for e in scene["elements"] if isinstance(e, dict)):
                    el["id"] = "movement"
        ms = data.get("movement_style") or data.get("mouse_style")
        if isinstance(ms, dict) and ms.get("motion") == "comet":
            ms["motion"] = "mousepad"
    if "mouse_style" in data and "movement_style" not in data:     # renamed (Session 18)
        data["movement_style"] = data.pop("mouse_style")
    data.pop("mouse_style", None)
    old_controller = data.pop("controller", None)                   # Session 18 pages -> Session 19 scene
    out = copy.deepcopy(DEFAULTS)
    for section, vals in data.items():
        if section == "scene":
            out["scene"] = vals if isinstance(vals, dict) and isinstance(vals.get("elements"), list) else out["scene"]
        elif isinstance(vals, dict) and isinstance(out.get(section), dict):
            out[section].update(vals)
        else:
            out[section] = vals
    f = out["full"]
    if f.pop("split", False):
        f["element_sources"] = True
    for k in ("keyboard_port", "mouse_port"):
        f.pop(k, None)
    if isinstance(old_controller, dict) and old_controller.get("enabled"):
        f["enabled"] = True
        if not _kl.scene_has(out["scene"], "controller"):
            right = max((_kl.element_rect(e)[0] + _kl.element_rect(e)[2] for e in out["scene"]["elements"]), default=0)
            out["scene"]["elements"].append({"id": _kl.new_element_id(out["scene"], "controller"),
                                             "type": "controller", "x": right + 1.0, "y": 0.0, "scale": 1.0})
    out["defaults_rev"] = DEFAULTS_REV
    return out


def load() -> dict:
    try:
        return merged(json.loads(config_file().read_text()))
    except (OSError, ValueError):
        return merged({})


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)             # holds the OBS websocket password
    tmp.replace(config_file())


def helper_wanted(cfg: dict) -> bool:
    """Same rule as the daemon's overlay_wanted()."""
    return any(cfg.get(k, {}).get("enabled") for k in ("full", "simple", "replay"))


def pages(cfg: dict) -> dict:
    """{page: port} for every page that's on: full (the scene), simple (text
    list), movement (one movement view). Elements live at /el/<id> on the
    full page's port (element_urls)."""
    out = {}
    if cfg["full"]["enabled"]:
        out["full"] = int(cfg["full"]["port"])
    if cfg["simple"]["enabled"]:
        out["simple"] = int(cfg["simple"]["port"])
        if cfg["simple"].get("mouse_movement"):
            out["movement"] = int(cfg["simple"]["mouse_port"])
    return out


def urls(cfg: dict) -> dict:
    return {p: f"http://127.0.0.1:{port}/" for p, port in pages(cfg).items()}


def element_urls(cfg: dict) -> dict:
    """{element id: url} when "each element as its own source" is on."""
    if not (cfg["full"]["enabled"] and cfg["full"].get("element_sources")):
        return {}
    port = int(cfg["full"]["port"])
    return {e["id"]: f"http://127.0.0.1:{port}/el/{e['id']}" for e in cfg["scene"].get("elements", [])}


def parse_color(s: str):
    """"#rrggbb" / "#rrggbbaa" -> (r, g, b, a) ints."""
    s = (s or "").strip().lstrip("#")
    try:
        if len(s) == 6:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255
        if len(s) == 8:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16)
    except ValueError:
        pass
    return 0, 0, 0, 0
