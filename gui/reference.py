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
  Move by (x, y) over time_ seconds. easing: none, linear, in, out, inout.
  move_to=True: (x, y) is an absolute screen position (KDE/kdotool),
  corrected for pointer acceleration. async_=True returns immediately.
  Use the "Mouse position" readout to find coordinates.

wheel(amount)
  Scroll. Positive = up, negative = down.

ignore(target)
  Blocks REAL input from reaching anything else while on (a toggle --
  call again to turn it off). target: "keyboard" (everything except the
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
loops, if, variables, anything. Turn it OFF for the fast native path:
one primitive or macro call per line, literal arguments/parameters only,
no control flow. Both are far past the old ~16k/s tap ceiling; native
has the least per-line overhead.

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
