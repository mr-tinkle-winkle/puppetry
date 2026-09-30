"""
The keyboard + mouse picture, as data (no Qt): geometry in key units, the
input state it shows, and the math for the curved movement arrow and the
hold timers.

One source for every place the picture appears -- the Input Visualizer,
the Dictionary's key-name map, the OBS pages (served as JSON to the
browser, see overlay_web/) and the overlay video renderer -- so they
can't drift apart.
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Keys: (evdev code, KEY_ name, label, width in key units). None = half gap.
# ---------------------------------------------------------------------------


def _k(code, name, label=None, w=1.0):
    return (code, name, label if label is not None else name[4:], w)


_LETTER_CODES = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                         (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20, 22, 47,
                          17, 45, 21, 44)))


def _ltr(ch, w=1.0):
    return _k(_LETTER_CODES[ch], "KEY_" + ch, ch, w)


KEY_ROWS = [
    [_k(1, "KEY_ESC", "Esc"), None] + [_k(59 + i, f"KEY_F{i + 1}", f"F{i + 1}") for i in range(10)]
    + [_k(87, "KEY_F11", "F11"), _k(88, "KEY_F12", "F12")],
    [_k(41, "KEY_GRAVE", "`")] + [_k(2 + i, f"KEY_{(i + 1) % 10}", str((i + 1) % 10)) for i in range(10)]
    + [_k(12, "KEY_MINUS", "-"), _k(13, "KEY_EQUAL", "="), _k(14, "KEY_BACKSPACE", "Bksp", 2.0)],
    [_k(15, "KEY_TAB", "Tab", 1.5)] + [_ltr(c) for c in "QWERTYUIOP"]
    + [_k(26, "KEY_LEFTBRACE", "["), _k(27, "KEY_RIGHTBRACE", "]"), _k(43, "KEY_BACKSLASH", "\\", 1.5)],
    [_k(58, "KEY_CAPSLOCK", "Caps", 1.75)] + [_ltr(c) for c in "ASDFGHJKL"]
    + [_k(39, "KEY_SEMICOLON", ";"), _k(40, "KEY_APOSTROPHE", "'"), _k(28, "KEY_ENTER", "Enter", 2.25)],
    [_k(42, "KEY_LEFTSHIFT", "Shift", 2.25)] + [_ltr(c) for c in "ZXCVBNM"]
    + [_k(51, "KEY_COMMA", ","), _k(52, "KEY_DOT", "."), _k(53, "KEY_SLASH", "/"),
       _k(54, "KEY_RIGHTSHIFT", "Shift", 2.75)],
    [_k(29, "KEY_LEFTCTRL", "Ctrl", 1.25), _k(125, "KEY_LEFTMETA", "Meta", 1.25), _k(56, "KEY_LEFTALT", "Alt", 1.25),
     _k(57, "KEY_SPACE", "", 6.25), _k(100, "KEY_RIGHTALT", "Alt", 1.25), _k(126, "KEY_RIGHTMETA", "Meta", 1.25),
     _k(127, "KEY_COMPOSE", "Menu", 1.25), _k(97, "KEY_RIGHTCTRL", "Ctrl", 1.25)],
]
# navigation block: (row, column, key)
NAV_KEYS = [(1, 0, _k(110, "KEY_INSERT", "Ins")), (1, 1, _k(102, "KEY_HOME", "Home")),
            (1, 2, _k(104, "KEY_PAGEUP", "PgUp")), (2, 0, _k(111, "KEY_DELETE", "Del")),
            (2, 1, _k(107, "KEY_END", "End")), (2, 2, _k(109, "KEY_PAGEDOWN", "PgDn"))]
ARROW_KEYS = [(4, 1, _k(103, "KEY_UP", "↑")), (5, 0, _k(105, "KEY_LEFT", "←")),
              (5, 1, _k(108, "KEY_DOWN", "↓")), (5, 2, _k(106, "KEY_RIGHT", "→"))]

MOUSE_BUTTONS = {"BTN_LEFT": 272, "BTN_RIGHT": 273, "BTN_MIDDLE": 274, "BTN_SIDE": 275, "BTN_EXTRA": 276}
MOUSE_LABELS = {"BTN_LEFT": "L", "BTN_RIGHT": "R", "BTN_MIDDLE": "", "BTN_SIDE": "", "BTN_EXTRA": ""}

# Controller buttons (evdev codes; names by POSITION: south = bottom face
# button -- A on Xbox, Cross on PlayStation, B on Nintendo).
PAD_BUTTONS = {"BTN_SOUTH": 0x130, "BTN_EAST": 0x131, "BTN_NORTH": 0x133, "BTN_WEST": 0x134, "BTN_TL": 0x136,
               "BTN_TR": 0x137, "BTN_TL2": 0x138, "BTN_TR2": 0x139, "BTN_SELECT": 0x13A, "BTN_START": 0x13B,
               "BTN_MODE": 0x13C, "BTN_THUMBL": 0x13D, "BTN_THUMBR": 0x13E, "BTN_DPAD_UP": 0x220,
               "BTN_DPAD_DOWN": 0x221, "BTN_DPAD_LEFT": 0x222, "BTN_DPAD_RIGHT": 0x223}
PAD_LABELS = {
    "xbox": {"BTN_SOUTH": "A", "BTN_EAST": "B", "BTN_WEST": "X", "BTN_NORTH": "Y", "BTN_TL": "LB", "BTN_TR": "RB",
             "BTN_TL2": "LT", "BTN_TR2": "RT", "BTN_SELECT": "View", "BTN_START": "Menu", "BTN_MODE": "Xbox",
             "BTN_THUMBL": "LS", "BTN_THUMBR": "RS"},
    "playstation": {"BTN_SOUTH": "\u2715", "BTN_EAST": "\u25cb", "BTN_WEST": "\u25a1", "BTN_NORTH": "\u25b3",
                    "BTN_TL": "L1", "BTN_TR": "R1", "BTN_TL2": "L2", "BTN_TR2": "R2", "BTN_SELECT": "Share",
                    "BTN_START": "Options", "BTN_MODE": "PS", "BTN_THUMBL": "L3", "BTN_THUMBR": "R3"},
    "nintendo": {"BTN_SOUTH": "B", "BTN_EAST": "A", "BTN_WEST": "Y", "BTN_NORTH": "X", "BTN_TL": "L", "BTN_TR": "R",
                 "BTN_TL2": "ZL", "BTN_TR2": "ZR", "BTN_SELECT": "\u2212", "BTN_START": "+", "BTN_MODE": "Home",
                 "BTN_THUMBL": "LS", "BTN_THUMBR": "RS"},
}
for _scheme in PAD_LABELS.values():
    _scheme.update({"BTN_DPAD_UP": "\u2191", "BTN_DPAD_DOWN": "\u2193", "BTN_DPAD_LEFT": "\u2190",
                    "BTN_DPAD_RIGHT": "\u2192"})
# ABS_* code -> short axis name (triggers reported as GAS/BRAKE by some pads)
AXIS_NAMES = {0: "LX", 1: "LY", 3: "RX", 4: "RY", 2: "LT", 5: "RT", 16: "DPAD_X", 17: "DPAD_Y", 9: "RT", 10: "LT"}

CODE_TO_NAME: dict = {}
for _row in KEY_ROWS:
    for _key in _row:
        if _key:
            CODE_TO_NAME[_key[0]] = _key[1]
for _r, _c, _key in NAV_KEYS + ARROW_KEYS:
    CODE_TO_NAME[_key[0]] = _key[1]
CODE_TO_NAME.update({v: k for k, v in MOUSE_BUTTONS.items()})
CODE_TO_NAME.update({v: k for k, v in PAD_BUTTONS.items()})
LABELS = {key[1]: key[2] for row in KEY_ROWS for key in row if key}
LABELS.update({key[1]: key[2] for _r, _c, key in NAV_KEYS + ARROW_KEYS})
LABELS.update(MOUSE_LABELS)
LABELS.update(PAD_LABELS["xbox"])

ROW_GAP = 0.35          # extra space under the function row


def build_layout(show_function_row=True, show_nav=True, show_arrows=True, show_mouse=True, show_arrow=True,
                 show_keyboard=True) -> dict:
    """{"items": [...], "w": units, "h": units}. Each item:
    {"kind": "key"|"mouse_body"|"mouse_btn"|"wheel"|"arrow_box", "name", "label", "x", "y", "w", "h"}
    Coordinates in key units, top-left at (0, 0)."""
    items = []
    if show_keyboard:
        for ri, row in enumerate(KEY_ROWS):
            if ri == 0 and not show_function_row:
                continue
            x = 0.0
            y = ri + (ROW_GAP if ri > 0 else 0)
            for k in row:
                if k is None:
                    x += 0.5
                    continue
                items.append({"kind": "key", "name": k[1], "label": k[2], "x": x, "y": y, "w": k[3], "h": 1.0})
                x += k[3]
        clusters = (NAV_KEYS if show_nav else []) + (ARROW_KEYS if show_arrows else [])
        for ri, col, k in clusters:
            items.append({"kind": "key", "name": k[1], "label": k[2], "x": 15.5 + col, "y": ri + ROW_GAP,
                          "w": 1.0, "h": 1.0})
    mx = 19.2 if (show_keyboard and (show_nav or show_arrows)) else (15.6 if show_keyboard else 0.3)
    if show_mouse:
        bw, bh = 2.6, 3.3
        by = 0.35
        items.append({"kind": "mouse_body", "name": "", "label": "", "x": mx, "y": by, "w": bw, "h": bh})
        g = bw * 0.06
        half = bw * 0.38
        items.append({"kind": "mouse_btn", "name": "BTN_LEFT", "label": "L", "x": mx + g, "y": by + g,
                      "w": half, "h": bh * 0.42})
        items.append({"kind": "mouse_btn", "name": "BTN_RIGHT", "label": "R", "x": mx + bw * 0.56, "y": by + g,
                      "w": half, "h": bh * 0.42})
        items.append({"kind": "wheel", "name": "BTN_MIDDLE", "label": "", "x": mx + bw * 0.42,
                      "y": by + bh * 0.42 * 0.25, "w": bw * 0.16, "h": bh * 0.42 * 0.6})
        items.append({"kind": "mouse_btn", "name": "BTN_SIDE", "label": "", "x": mx - 0.18, "y": by + bh * 0.45,
                      "w": 0.3, "h": 0.55})
        items.append({"kind": "mouse_btn", "name": "BTN_EXTRA", "label": "", "x": mx - 0.18,
                      "y": by + bh * 0.45 + 0.65, "w": 0.3, "h": 0.55})
    if show_arrow:
        ax = mx - 0.3 if show_mouse else mx
        items.append({"kind": "arrow_box", "name": "", "label": "", "x": ax, "y": 3.95 if show_mouse else 0.35,
                      "w": 3.2, "h": 2.4})
    if not items:
        return {"items": [], "w": 1.0, "h": 1.0}
    minx = min(i["x"] for i in items)
    miny = min(i["y"] for i in items)
    for i in items:
        i["x"] -= minx
        i["y"] -= miny
    w = max(i["x"] + i["w"] for i in items)
    h = max(i["y"] + i["h"] for i in items)
    return {"items": items, "w": w, "h": h}


def build_controller_layout() -> dict:
    """A gamepad, ~10 x 6.4 key units. Item kinds: pad_body, shoulder,
    trigger (fills with its axis), stick (base; knob follows its axes;
    pressed = stick click), dpad (four arms; buttons or the hat axes),
    pad_btn (face buttons, round), small (select / start / guide)."""
    it = []

    def add(kind, name, x, y, w, h, **kw):
        it.append({"kind": kind, "name": name, "label": "", "x": x, "y": y, "w": w, "h": h, **kw})
    add("pad_body", "", 0.0, 1.5, 10.0, 4.2)          # drawn first: everything else sits on it
    add("trigger", "BTN_TL2", 0.9, 0.0, 1.6, 0.85, axis="LT")
    add("trigger", "BTN_TR2", 7.5, 0.0, 1.6, 0.85, axis="RT")
    add("shoulder", "BTN_TL", 0.6, 0.93, 2.2, 0.55)
    add("shoulder", "BTN_TR", 7.2, 0.93, 2.2, 0.55)
    add("stick", "BTN_THUMBL", 1.1, 2.1, 1.8, 1.8, ax="LX", ay="LY")
    add("stick", "BTN_THUMBR", 5.9, 3.9, 1.8, 1.8, ax="RX", ay="RY")
    # d-pad: a plus sign
    cx, cy, a = 3.3, 4.8, 0.55
    add("dpad", "BTN_DPAD_UP", cx - a / 2, cy - a * 1.5, a, a, hat=("DPAD_Y", -1))
    add("dpad", "BTN_DPAD_DOWN", cx - a / 2, cy + a * 0.5, a, a, hat=("DPAD_Y", 1))
    add("dpad", "BTN_DPAD_LEFT", cx - a * 1.5, cy - a / 2, a, a, hat=("DPAD_X", -1))
    add("dpad", "BTN_DPAD_RIGHT", cx + a * 0.5, cy - a / 2, a, a, hat=("DPAD_X", 1))
    # face buttons: a diamond
    fx, fy, r = 8.0, 3.0, 0.62
    add("pad_btn", "BTN_NORTH", fx - r / 2, fy - r * 1.55, r, r)
    add("pad_btn", "BTN_SOUTH", fx - r / 2, fy + r * 0.55, r, r)
    add("pad_btn", "BTN_WEST", fx - r * 1.55, fy - r / 2, r, r)
    add("pad_btn", "BTN_EAST", fx + r * 0.55, fy - r / 2, r, r)
    add("small", "BTN_SELECT", 3.6, 2.7, 0.9, 0.45)
    add("small", "BTN_START", 5.5, 2.7, 0.9, 0.45)
    add("small", "BTN_MODE", 4.6, 2.0, 0.8, 0.8)
    return {"items": it, "w": 10.0, "h": 6.4}


PIECES = ("full", "keyboard", "mouse", "controller")


def build_piece(piece: str, style: dict) -> dict:
    """full = keyboard + mouse (+ arrow); keyboard / mouse / controller alone."""
    if piece == "controller":
        return build_controller_layout()
    if piece == "keyboard":
        return build_layout(style.get("show_function_row", True), style.get("show_nav", True),
                            style.get("show_arrows", True), False, False, True)
    if piece == "mouse":
        return build_layout(True, True, True, True, style.get("show_arrow", True), False)
    return layout_for_style(style)


def combine_layouts(a: dict, b: dict, gap: float = 1.0) -> dict:
    """b placed to the right of a, both vertically centered."""
    items = [dict(i) for i in a["items"]]
    h = max(a["h"], b["h"])
    for i in items:
        i["y"] += (h - a["h"]) / 2
    for i in b["items"]:
        j = dict(i)
        j["x"] += a["w"] + gap
        j["y"] += (h - b["h"]) / 2
        items.append(j)
    return {"items": items, "w": a["w"] + gap + b["w"], "h": h}


# ---------------------------------------------------------------------------
# Hold timers
# ---------------------------------------------------------------------------
def format_hold(seconds: float) -> str:
    """0.2344 -> "0.234", 75.5 -> "1:15.500" (millisecond accuracy)."""
    ms = max(0, int(seconds * 1000 + 1e-6))
    if ms < 60000:
        return f"{ms // 1000}.{ms % 1000:03d}"
    m, rest = divmod(ms, 60000)
    return f"{m}:{rest // 1000:02d}.{rest % 1000:03d}"


# ---------------------------------------------------------------------------
# Input state
# ---------------------------------------------------------------------------
MOVE_GAP = 0.15         # seconds of stillness that end a movement burst


class KbmState:
    """What the picture shows at a moment. Times are seconds on any single
    clock (perf_counter in the app, epoch seconds in the renderer)."""

    def __init__(self):
        self.held: dict = {}        # name -> press time (real input)
        self.out_held: dict = {}    # name -> press time (macro output)
        self.released: dict = {}    # name -> release time (for fade-out)
        self.wheel_dir = 0
        self.wheel_t = -1e18
        self.wheel_src = "r"
        self.burst: list = []       # [(t, cum_x, cum_y)] current / last movement
        self.burst_src = "r"        # whose movement it is: r real, m macro
        self.last_move_t = -1e18
        self.axes: dict = {}        # controller axes, real: "LX" -> -1..1 / 0..1
        self.out_axes: dict = {}    # ... set by a macro (virtual controller)
        self.revision = 0

    def key(self, name: str, down: bool, t: float, src: str = "r") -> None:
        d = self.held if src == "r" else self.out_held
        if down:
            d.setdefault(name, t)
            self.released.pop(name, None)
        else:
            if d.pop(name, None) is not None and src == "r":
                self.released[name] = t
        self.revision += 1

    def wheel(self, n: float, t: float, src: str = "r") -> None:
        if n:
            self.wheel_dir = 1 if n > 0 else -1
            self.wheel_t = t
            self.wheel_src = src
            self.revision += 1

    def axis(self, name: str, value: float, t: float, src: str = "r") -> None:
        d = self.axes if src == "r" else self.out_axes
        if value:
            d[name] = value
        else:
            d.pop(name, None)
        self.revision += 1

    def axis_value(self, name: str) -> tuple:
        """(value, src): the real axis when it's off-center, else the macro's."""
        v = self.axes.get(name, 0.0)
        if v:
            return v, "r"
        return self.out_axes.get(name, 0.0), "m"

    def move(self, dx: float, dy: float, t: float, src: str = "r") -> None:
        if not (dx or dy):
            return
        if not self.burst or t - self.last_move_t > MOVE_GAP or src != self.burst_src:
            self.burst = [(t, 0.0, 0.0)]
            self.burst_src = src
        _t, cx, cy = self.burst[-1]
        self.burst.append((t, cx + dx, cy + dy))
        if len(self.burst) > 4000:                       # keep it bounded; shape survives thinning
            self.burst = self.burst[:1] + self.burst[1::2]
        self.last_move_t = t
        self.revision += 1

    def clear(self) -> None:
        self.__init__()


# ---------------------------------------------------------------------------
# The curved movement arrow
# ---------------------------------------------------------------------------
def _pos_at(points, t):
    if t <= points[0][0]:
        return points[0][1], points[0][2]
    for i in range(1, len(points)):
        t1, x1, y1 = points[i]
        if t1 >= t:
            t0, x0, y0 = points[i - 1]
            f = 0 if t1 == t0 else (t - t0) / (t1 - t0)
            return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
    return points[-1][1], points[-1][2]


def arrow_curve(points: list):
    """Cubic Bezier through the movement's start, its positions at 1/3 and
    2/3 of the burst's DURATION, and its end -- so the arrow points along
    the net direction, its length follows the distance, and it bends where
    (and when) the path did: up-then-left curves the start up, left-then-up
    curves the end up. -> {"p0","c1","c2","p3","mag"} or None."""
    if len(points) < 2:
        return None
    x3, y3 = points[-1][1], points[-1][2]
    mag = math.hypot(x3, y3)
    if mag < 2:
        return None
    t0, t1 = points[0][0], points[-1][0]
    if t1 - t0 <= 0:
        b1, b2 = (x3 / 3, y3 / 3), (2 * x3 / 3, 2 * y3 / 3)
    else:
        b1 = _pos_at(points, t0 + (t1 - t0) / 3)
        b2 = _pos_at(points, t0 + 2 * (t1 - t0) / 3)
    # control points of the cubic that passes through b1 at u=1/3, b2 at 2/3
    c1 = ((18 * b1[0] - 9 * b2[0] + 2 * x3) / 6, (18 * b1[1] - 9 * b2[1] + 2 * y3) / 6)
    c2 = ((-9 * b1[0] + 18 * b2[0] - 5 * x3) / 6, (-9 * b1[1] + 18 * b2[1] - 5 * y3) / 6)
    return {"p0": (0.0, 0.0), "c1": c1, "c2": c2, "p3": (x3, y3), "mag": mag}


def fit_arrow(curve: dict, box_w: float, box_h: float, full_distance: float = 600.0, min_frac: float = 0.25):
    """Scale + center a curve in a box. The arrow's straight-line length
    grows with the square root of the distance moved (full box at
    `full_distance` pixels), never shorter than `min_frac` of the box.
    -> (p0, c1, c2, p3) in box coordinates."""
    pts = [curve["p0"], curve["c1"], curve["c2"], curve["p3"]]
    mag = curve["mag"]
    avail = min(box_w, box_h) * 0.85
    length = avail * max(min_frac, min(1.0, math.sqrt(mag / max(full_distance, 1.0))))
    s = length / mag
    xs = [p[0] * s for p in pts]
    ys = [p[1] * s for p in pts]
    ext = max(max(xs) - min(xs), 1e-9), max(max(ys) - min(ys), 1e-9)
    shrink = min(1.0, box_w * 0.9 / ext[0], box_h * 0.9 / ext[1])
    xs = [x * shrink for x in xs]
    ys = [y * shrink for y in ys]
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2
    return [(x - cx + box_w / 2, y - cy + box_h / 2) for x, y in zip(xs, ys)]


def arrow_head_dir(pts):
    """Unit direction at the arrow's tip (the curve's end tangent)."""
    p3 = pts[3]
    for q in (pts[2], pts[1], pts[0]):
        dx, dy = p3[0] - q[0], p3[1] - q[1]
        n = math.hypot(dx, dy)
        if n > 1e-6:
            return dx / n, dy / n
    return 1.0, 0.0


def layout_for_style(style: dict) -> dict:
    return build_layout(style.get("show_function_row", True), style.get("show_nav", True),
                        style.get("show_arrows", True), style.get("show_mouse", True),
                        style.get("show_arrow", True), style.get("show_keyboard", True))


def pixel_size(style: dict) -> tuple:
    """(width, height) in pixels of the full picture at the style's key size."""
    lay = layout_for_style(style)
    u, pad = float(style.get("unit", 48)), float(style.get("padding", 0))
    return int(math.ceil(lay["w"] * u + 2 * pad)), int(math.ceil(lay["h"] * u + 2 * pad))


def layout_pixel_size(layout: dict, style: dict) -> tuple:
    u, pad = float(style.get("unit", 48)), float(style.get("padding", 0))
    return int(math.ceil(layout["w"] * u + 2 * pad)), int(math.ceil(layout["h"] * u + 2 * pad))


def piece_pixel_size(piece: str, style: dict) -> tuple:
    return layout_pixel_size(build_piece(piece, style), style)
