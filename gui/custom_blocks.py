"""
The custom block library ("Create a custom block"), shared by every macro.

Stored at ~/.config/macro-daemon/custom_blocks.json:

    {"blocks": [{"name": "click at", "func": "click_at", "category": "Output",
                 "color": "#9b6ad6", "args": [{"name": "x", "default": "0",
                 "source": "any", "options": []}, ...],
                 "template": "move_mouse({x}, {y}, move_to=True)\\ntap(BTN_LEFT)",
                 "python_on": false, "description": "",
                 "code": "<generated: arguments(...) + template>"}]}

The daemon only reads "name" (sanitized -> the callable name), "code" and
"python_on": each custom block is registered like a macro that has no
combo, so any macro can call it -- `click_at(100, 200)` -- on either the
native or the embedded-Python path. String arguments are converted to the
type of each argument's default (see coerce_macro_arg in the daemon).
"""
from __future__ import annotations

import json
import re

import puppetry_config as cfg
from block_model import CustomArg, CustomDef, custom_body_code

CATEGORIES = ["Output", "Timing & control", "Conditions", "Real input", "Variables", "My blocks", "Other"]
ARG_SOURCES = [("any", "Any value"), ("keys", "Keyboard keys"), ("mouse", "Mouse buttons"),
               ("keys_mouse", "Keyboard + mouse"), ("list", "Custom list")]
MOUSE_BUTTONS = ["BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK"]


def blocks_file():
    return cfg.CONFIG_DIR / "custom_blocks.json"


def func_name(label: str) -> str:
    """"Click at!" -> "click_at" (a valid identifier; same rule the daemon
    uses to sanitize names, lower-cased and trimmed)."""
    ident = re.sub(r"\W", "_", label.strip()).strip("_")
    ident = re.sub(r"_+", "_", ident)
    if not ident or ident[0].isdigit():
        ident = "block_" + ident
    return ident


def to_dict(d: CustomDef) -> dict:
    return {"name": d.func, "label": d.name, "func": d.func, "category": d.category, "color": d.color,
            "args": [{"name": a.name, "default": a.default, "source": a.source, "options": list(a.options)}
                     for a in d.args],
            "template": d.template, "python_on": d.python_on, "description": d.description,
            "code": custom_body_code(d)}


def from_dict(x: dict) -> CustomDef:
    return CustomDef(
        name=x.get("label") or x.get("name", ""), func=x.get("func") or x.get("name", ""),
        category=x.get("category", "My blocks"), color=x.get("color", "#9b6ad6"),
        args=[CustomArg(a.get("name", "arg"), a.get("default", "0"), a.get("source", "any"), list(a.get("options") or []))
              for a in x.get("args", [])],
        template=x.get("template", ""), python_on=bool(x.get("python_on", False)),
        description=x.get("description", ""))


def load() -> dict:
    """{func_name: CustomDef}"""
    try:
        data = json.loads(blocks_file().read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for x in data.get("blocks", []):
        d = from_dict(x)
        if d.func:
            out[d.func] = d
    return out


def save(defs: dict) -> None:
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    blocks_file().write_text(json.dumps({"blocks": [to_dict(d) for d in defs.values()]}, indent=2))


def check(d: CustomDef) -> tuple[bool, str]:
    """Compile the block's code exactly the way the daemon will."""
    return cfg.check_macro({"name": d.func, "code": custom_body_code(d), "python_on": d.python_on,
                            "simplified_names": False})


def arg_options(a: CustomArg, key_names: list) -> "list | None":
    if a.source == "mouse":
        return MOUSE_BUTTONS
    if a.source == "list":
        return list(a.options)
    return None


def arg_completions(a: CustomArg, key_names: list) -> list:
    if a.source == "keys":
        return [k for k in key_names if k.startswith("KEY_")]
    if a.source == "keys_mouse":
        return list(key_names)
    return []
