"""In-editor reference text (the old GTK "Function reference" and
"Simplified name reference" expanders), updated for the C++ daemon."""
from __future__ import annotations

import puppetry_config as cfg

DICTIONARY_TEXT = """\
tap(key, time_=0.1)
  Key/button down, wait time_, up. Works for keys and mouse buttons
  (tap(KEY_A), tap(BTN_LEFT)).

kd(key) / ku(key)
  Down only / up only -- pair them. Keys and mouse buttons.

combo(*keys, time_=0.1)
  A chord: presses every key in order, waits time_, releases in reverse,
  e.g. combo(KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_S). Always pass time_ as a
  keyword (a bare number can't be told apart from a key code).

type(text, time_per_letter=0.05, async_=False)
  Types a string (US QWERTY, shifted symbols handled). Unsupported
  characters are skipped. async_=True returns immediately.

wait(time_, precise=False)
  Pause time_ seconds (scaled by speed()). Tiny waits are spun on a
  high-resolution clock instead of slept, and longer ones sleep to an
  absolute deadline and then spin the last ~100us -- so waits land within
  about a microsecond instead of being floored at the kernel's ~30-75us
  sleep cost. Consecutive waits share a timeline: the few microseconds
  each line in between costs are absorbed instead of piling up as drift.
  precise=True spins the last 1ms instead, which also rides out
  scheduler hiccups on a busy system (more CPU during the wait).

speed(multiplier)
  Scales every duration from here on (wait, tap/combo hold, move_mouse,
  type) for the rest of THIS run of this macro, including macros it
  calls. Resets to 1 at the start of every run/loop iteration.

move_mouse(x_pixels, y_pixels, time_=0.25, easing="inout", async_=False, move_to=False)
move_mouse(position, ...)   -- a saved getMousePosition() (absolute)
  Move by (x, y) over time_ seconds. easing: none, linear, in, out, inout.
  move_to=True: (x, y) is an absolute screen position (KDE/kdotool),
  corrected for pointer acceleration. async_=True returns immediately.
  Use the "Mouse position" readout to find coordinates.

wheel(amount)
  Scroll. Positive = up, negative = down.

ignore(what)
  Blocks REAL input from reaching anything else while on (a toggle --
  call again to turn it off). what: "keyboard" (everything except the
  abort key), "mouse_buttons", "mouse_movement", or "mouse" (both).
  Grabs the real device; the abort key always force-releases it.

ignore_keys(*keys)
  Like ignore(), but for specific keys/buttons only, e.g.
  ignore_keys(KEY_W, BTN_LEFT). A toggle per key.

actAs(key, ignore, *acting_keys)
  From now on, every press of `key` ALSO presses the acting key(s), e.g.
  actAs(BTN_LEFT, False, KEY_Q, KEY_E). ignore=True suppresses the
  original key's own press. Runtime-only (a daemon restart clears it),
  global across profiles, cleared by the abort key. Calling it again
  with the exact same arguments turns it back off. If the key is held
  when you change it, the change applies from its next press.

checkpoint()
  Does nothing at all when the macro runs -- a pure marker. The editor's
  "Clear macro before transcribing" option looks for the last checkpoint()
  line and, when one's there, clears only what comes after it instead of
  the whole macro. Drop one in by hand, or use the Checkpoint key while
  transcribing.

waitForPress(button="any", repress=False)
  Waits until you press that real key/button -- or, with no button (or
  "any"/"all"), ANY key or mouse button -- and returns the key that was
  pressed:  b = waitForPress()  then tap(b) etc. repress=True swallows
  that press so nothing else sees it.

waitForReactivation(repress=False)
  Waits until this macro's own key combo is pressed again. That press
  wakes this run instead of restarting (or toggling off) the macro.
  repress=True swallows it so nothing else sees it.

getMousePosition()
  The mouse position as (x, y). pos = getMousePosition(), then pos.x /
  pos.y (or pos[0] / pos[1], or x, y = pos). getMousePosition.x reads it
  directly. move_mouse(pos) moves back to a saved position. Needs kdotool
  (KDE Plasma).

getButtonsHeld()
  A list of every real key/button held right now, e.g.
  if KEY_A in getButtonsHeld(): ...

command(cmd, *args)
  Runs a shell command fire-and-forget (/bin/sh). Extra args are
  shell-quoted into {0}, {1}, ... placeholders:
      command("notify-send {0} {1}", "Title", "a message")
  Output/errors go to `journalctl --user -u macro-daemon`.

arguments(name=default, ...)  -- FIRST LINE ONLY
  Declares parameters, e.g. arguments(hits=3, key=KEY_A). Every
  parameter needs a default. CLI/other-macro arguments arrive as
  STRINGS -- use int(...)/float(...) where a number is needed.

Other macros are callable by name (spaces/punctuation become
underscores): "Flick and Click" -> Flick_and_Click(hits=5).

Every KEY_* and BTN_* name is available directly (KEY_A, BTN_LEFT).

Run as embedded Python (on by default): the macro is real Python --
anything goes, including imports. Turn it OFF for the fast native path:
Puppetry's own interpreter for the everyday subset -- variables, math,
comparisons, and/or/not, if/elif/else, while, for, break/continue, your
own functions (def/return), lists/tuples, int()/float()/str()/len()/
min()/max()/abs()/round()/range()/print(), every primitive, and other
macros. No imports, classes, try, or methods like "a".upper(). Both are
far past the old ~16k/s tap ceiling; native has the least overhead.

Arguments passed to a macro (CLI, another macro) arrive as text and are
converted to the type of that parameter's default: arguments(hits=3)
gets the number 5 from "5", arguments(key=KEY_A) gets KEY_B's code.

From outside Puppetry: `puppetry --name="Macro Name" [args...]` triggers
a macro on the running daemon; `puppetry --abort` = the abort key."""


def simplified_names_reference_text() -> str:
    tables = cfg.name_tables()
    grouped: dict[str, list[str]] = {}
    for simple, real in tables.get("simplified", {}).items():
        grouped.setdefault(real, []).append(simple)
    if not grouped:
        return "(unavailable -- couldn't run puppetry-daemon --dump-names)"
    lines = []
    for real in sorted(grouped):
        shown = sorted(set(grouped[real]), key=lambda s: (not s.isupper(), s))
        lines.append(f"{' / '.join(shown):<24} -> {real}")
    return "\n".join(lines)


def alias_targets() -> list[str]:
    return sorted(set(cfg.name_tables().get("simplified", {}).keys()))


# ---------------------------------------------------------------------------
# Structured primitive table
#
# One entry per primitive the daemon registers (python_embed.cpp's FC(...)
# table / native_vm.cpp's compile_primitive()). The block editor builds its
# palette, block labels, sockets and tooltips from this, and the parser
# uses it to map calls onto blocks. A test cross-checks the names against
# the daemon source so this can't silently drift.
#
# Param fields:
#   name      -- the real Python keyword name (what the daemon accepts)
#   default   -- None = required; otherwise the default's SOURCE text
#   kind      -- "key" | "number" | "bool" | "choice" | "string" | "keys" | "values"
#                ("keys"/"values" = a *varargs tail: one socket per value,
#                plus an empty one that grows the list)
#   caption   -- short label drawn before the socket ("" = none)
#   choices   -- for kind="choice": source-text options
#   kw_only   -- can only be passed as a keyword (combo's time_)
#   vararg    -- True for the *args tail
#   doc       -- tooltip for the socket
# category: "input" | "output" | "neutral" (orange / blue / grey).
# label: plain-English block text (actAs -> "act as").
# reporter: returns a value -- shown as a round reporter, not a statement.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as _field


@dataclass(frozen=True)
class Param:
    name: str
    default: "str | None" = None
    kind: str = "number"
    caption: str = ""
    choices: tuple = ()
    kw_only: bool = False
    vararg: bool = False
    doc: str = ""

    @property
    def required(self) -> bool:
        return self.default is None and not self.vararg


@dataclass(frozen=True)
class Primitive:
    name: str
    params: tuple
    category: str
    summary: str = ""
    label: str = ""
    reporter: bool = False

    @property
    def title(self) -> str:
        return self.label or self.name


_REPRESS = Param("repress", "False", kind="bool", caption="block it",
                 doc="True: the press is swallowed -- nothing else (no app, no other macro) sees it.")

PRIMITIVES: tuple = (
    Primitive("tap", (Param("key", kind="key", doc="The key or mouse button, e.g. KEY_A or BTN_LEFT."),
                      Param("time_", "0.1", caption="hold", doc="Seconds to hold it down.")),
              "output", "Press and release a key or mouse button.", "tap"),
    Primitive("kd", (Param("key", kind="key", doc="The key or mouse button to press down."),), "output",
              "Press a key/button down and keep holding it -- pair it with key up.", "key down"),
    Primitive("ku", (Param("key", kind="key", doc="The key or mouse button to let go of."),), "output",
              "Let go of a key/button that key down pressed.", "key up"),
    Primitive("combo", (Param("keys", kind="keys", vararg=True, doc="Keys pressed in this order, released in reverse."),
                        Param("time_", "0.1", caption="hold", kw_only=True, doc="Seconds to hold them all down.")),
              "output", "Press several keys together (like Ctrl+C), then release them.", "combo"),
    Primitive("type", (Param("text", kind="string", doc="The text to type, in quotes."),
                       Param("time_per_letter", "0.05", caption="per letter", doc="Seconds between letters."),
                       Param("async_", "False", kind="bool", caption="in background",
                             doc="True: keep going while it types.")),
              "output", "Type out some text.", "type"),
    Primitive("move_mouse", (Param("x_pixels", caption="x", doc="Pixels right (negative = left), or a screen x with absolute on. "
                                                          "Can also be a saved mouse position."),
                             Param("y_pixels", caption="y", doc="Pixels down (negative = up), or a screen y with absolute on."),
                             Param("time_", "0.25", caption="over", doc="Seconds the movement takes."),
                             Param("easing", '"inout"', kind="choice", caption="easing",
                                   choices=('"none"', '"linear"', '"in"', '"out"', '"inout"'),
                                   doc="How the speed changes along the way."),
                             Param("async_", "False", kind="bool", caption="in background",
                                   doc="True: keep going while it moves."),
                             Param("move_to", "False", kind="bool", caption="absolute",
                                   doc="True: (x, y) is a spot on the screen instead of a distance.")),
              "output", "Move the mouse by (x, y) -- or to (x, y) with absolute on.", "move mouse"),
    Primitive("wheel", (Param("amount", doc="Clicks to scroll. Positive = up, negative = down."),), "output",
              "Scroll the mouse wheel.", "scroll"),
    Primitive("command", (Param("cmd", kind="string", doc="The shell command, in quotes. {0}, {1}... are filled from the extra values."),
                          Param("args", kind="values", caption="with", vararg=True,
                                doc="Values for {0}, {1}, ... (shell-quoted for you).")),
              "output", "Run a shell command (doesn't wait for it to finish).", "run command"),
    Primitive("wait", (Param("time_", caption="", doc="Seconds to wait."),
                       Param("precise", "False", kind="bool", caption="precise",
                             doc="True: spin the last millisecond for extra accuracy (uses more CPU).")),
              "neutral", "Pause for this many seconds.", "wait"),
    Primitive("speed", (Param("multiplier", doc="2 = twice as fast, 0.5 = half speed."),), "neutral",
              "Speed up or slow down every wait/hold/move after this, for the rest of this run.", "speed"),
    Primitive("checkpoint", (), "neutral",
              "A marker: \"Clear macro before transcribing\" only clears what comes after the last one. "
              "Does nothing when the macro runs.", "checkpoint"),
    Primitive("ignore", (Param("what", kind="choice", choices=('"keyboard"', '"mouse_buttons"', '"mouse_movement"',
                                                              '"mouse"'),
                               doc="Which real input to block (or unblock -- it's a toggle)."),),
              "input", "Toggle blocking your real keyboard/mouse from reaching anything else.", "ignore"),
    Primitive("ignore_keys", (Param("keys", kind="keys", vararg=True, doc="Keys/buttons to block (or unblock)."),),
              "input", "Toggle blocking specific real keys/buttons.", "ignore keys"),
    Primitive("actAs", (Param("key", kind="key", doc="The real key/button being remapped."),
                        Param("ignore", kind="bool", caption="suppress original",
                              doc="True: the original key's own press is blocked."),
                        Param("acting", kind="keys", caption="also press", vararg=True,
                              doc="Keys/buttons pressed whenever the key is.")),
              "input", "From now on, pressing the key also presses these (run again with the same values to undo).",
              "act as"),
    Primitive("waitForPress", (Param("button", '"any"', kind="key",
                                     doc="The real key or mouse button to wait for -- leave it empty (or \"any\") "
                                         "for any key or button."), _REPRESS),
              "input", "Wait here until you press this key/button (or any). As a value, it's the key that was "
                       "pressed: button = wait for press.", "wait for press"),
    Primitive("waitForReactivation", (_REPRESS,), "input",
              "Wait here until this macro's key combo is pressed again (that press doesn't restart or stop it).",
              "wait for reactivation"),
    Primitive("getMousePosition", (), "input",
              "Where the mouse is: (x, y). Use .x / .y for one number, or move mouse back to a saved one.",
              "mouse position", reporter=True),
    Primitive("getButtonsHeld", (), "input", "Every real key/button held down right now.", "buttons held",
              reporter=True),
)

PRIMITIVES_BY_NAME: dict = {p.name: p for p in PRIMITIVES}


def primitive_names() -> list[str]:
    return [p.name for p in PRIMITIVES]


# ---------------------------------------------------------------------------
# Dictionary page data. The per-primitive prose above is parsed (rather than
# duplicated) so the page, the palette tooltips and the plain-text blob can
# never disagree.
# ---------------------------------------------------------------------------
import re as _re


def parse_dictionary() -> tuple:
    """-> (entries, notes). An entry is {name, signature, description,
    category}; `notes` are the trailing paragraphs that describe no single
    command (how other macros are called, native vs Python, the CLI)."""
    entries, notes = [], []
    for para in DICTIONARY_TEXT.strip().split("\n\n"):
        lines = para.split("\n")
        head = [l for l in lines if not l.startswith(" ")][:]
        body = [l.strip() for l in lines if l.startswith(" ")]
        first_head = next((i for i, l in enumerate(lines) if l.startswith(" ")), len(lines))
        head = lines[:first_head]
        if not body:
            notes.append(" ".join(l.strip() for l in lines))
            continue
        names = _re.findall(r"\b([A-Za-z_]\w*)\(", head[0])
        name = names[0] if names else head[0].split()[0]
        cat = "neutral"
        for n in names:
            if n in PRIMITIVES_BY_NAME:
                cat = PRIMITIVES_BY_NAME[n].category
                break
        entries.append({"name": name, "names": names or [name], "signature": "\n".join(head),
                        "description": "\n".join(body), "category": cat})
    return entries, notes


# Block-only building blocks (control flow, variables, conditions) -- they
# exist in both the palette and plain code.
LANGUAGE_ENTRIES: tuple = (
    ("repeat N times", "for _ in range(N):", "Run the blocks inside N times.", "neutral"),
    ("for each", "for i in range(10):", "Run the blocks inside once per item (a number range or a list).", "neutral"),
    ("while", "while condition:", "Keep running the blocks inside as long as the condition is true.", "neutral"),
    ("if / else", "if condition: ... else: ...", "Run one set of blocks or the other, depending on the condition.", "neutral"),
    ("x is [equal to / not equal to / greater than / less than / ...] y", "x == y   x != y   x > y   x < y   x >= y   x <= y",
     "A true/false value. Green = true, red = false.", "neutral"),
    ("key is held", "KEY_A in getButtonsHeld()", "True while that real key or button is down.", "input"),
    ("set / change", "x = 0   x += 1", "Give a variable a value, or add to / subtract from it.", "neutral"),
    ("create function", "def name(a, b): ...", "A block of steps you can run again by name -- only inside this macro. "
                        "Use return to hand back a value.", "function"),
    ("run macro", "Other_Macro(arg)", "Run another macro by name, optionally with arguments.", "neutral"),
    ("true / false", "True   False", "The two yes/no values.", "neutral"),
    ("note", "# text", "A comment. Notes can sit anywhere on the canvas and are saved with the macro.", "neutral"),
    ("custom code", "(anything)", "Any code the blocks can't show; it stays exactly as written.", "custom"),
    ("create a custom block", "(the button under My blocks)", "Make your own block from a code template with arguments; "
                              "it shows up in every macro.", "custom"),
)


def signature_of(p: Primitive) -> str:
    parts = []
    for a in p.params:
        if a.vararg:
            parts.append(f"*{a.name}")
        elif a.default is None:
            parts.append(a.name)
        else:
            parts.append(f"{a.name}={a.default}")
    return f"{p.name}({', '.join(parts)})"


def key_alias_rows() -> list:
    """[(shown names, real KEY_/BTN_ name)] from the daemon's own tables."""
    grouped: dict = {}
    for simple, real in cfg.name_tables().get("simplified", {}).items():
        grouped.setdefault(real, []).append(simple)
    return [(" / ".join(sorted(set(v), key=lambda x: (not x.isupper(), x))), real) for real, v in sorted(grouped.items())]
