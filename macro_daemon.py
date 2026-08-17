#!/usr/bin/env python3
"""
macro_daemon.py — config-driven evdev/uinput macro daemon

Runs as a systemd --user service. Reads its entire setup (device paths,
active profile, and that profile's macros) from JSON config files under
~/.config/macro-daemon/. A future GTK app edits those files and restarts
this service on save -- this script has no live-reload logic and isn't
meant to need any; a restart is fast and config is the single source of
truth.

DEVICE ACCESS
-------------
Same design as before: this never grab()s your physical keyboard/mouse,
so all normal input keeps flowing to every app untouched. It only reads
the event stream to detect combos, and emits synthetic input through one
virtual uinput device when a macro fires.

CONFIG LAYOUT
-------------
~/.config/macro-daemon/state.json
    {
      "keyboard_path": "/dev/input/event13",
      "mouse_path": "/dev/input/event0",
      "active_profile": "profile_1",
      "autosave": false
    }
    keyboard_path/mouse_path are optional -- if null, missing, or the
    saved path no longer exists, the daemon auto-detects both by
    capability (full A-Z range = keyboard; BTN_LEFT (+ REL_X/REL_Y if
    available) = mouse) every time it starts. Set them explicitly here
    (or via the GUI's Detect buttons) only to override that.

~/.config/macro-daemon/macros.json
    -- THE macro definitions, shared across every profile. A profile
    -- does not have its own copy of a macro or its own repeat_mode --
    -- those are global. Profiles only toggle which of these are on.
    {
      "macros": [
        {
          "id": "a1b2c3",
          "name": "Flick and click",
          "description": "Smooth flick right, then click.",
          "repeat_mode": "none",   # "none" | "hold" | "toggle"
          "combo": ["KEY_LEFTCTRL", "KEY_F13"],
          "code": "move_mouse(400, 0)\ntap(BTN_LEFT)\n"
        }
      ]
    }

~/.config/macro-daemon/profiles/profile_1.json
    -- Purely a per-profile on/off mask over the shared macro list
    -- above, keyed by macro id. Missing id == disabled in this profile.
    {
      "name": "Profile 1",
      "enabled": { "a1b2c3": true }
    }

The "code" field is just the BODY of the macro -- no "def" line needed.
It's wrapped into a real function at load time and run in a namespace
that already has every primitive below, plus every KEY_*/BTN_* name as
a bare identifier (so macro text can write `kd(KEY_A)`, not
`kd(e.KEY_A)` or `kd("KEY_A")`).

REPEAT MODES
------------
none:   fires once per completed combo press.
hold:   loops the macro body continuously while the combo is held.
        On release, the CURRENT iteration is allowed to finish; no new
        iteration is started. (Deliberately not an instant hard-stop --
        see the module docstring in the project notes for why.)
toggle: first full-combo press starts the same kind of loop; a second
        press of the same combo stops it the same way -- finish the
        current iteration, don't start another.
"""

import asyncio
import json
import re
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

from evdev import InputDevice, UInput, ecodes as e, list_devices

# ---------------------------------------------------------------------------
# CONFIG PATHS
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "macro-daemon"
STATE_FILE = CONFIG_DIR / "state.json"
MACROS_FILE = CONFIG_DIR / "macros.json"
ALIASES_FILE = CONFIG_DIR / "aliases.json"
PROFILES_DIR = CONFIG_DIR / "profiles"

DEFAULT_PROFILE_NAMES = ["Profile 1", "Profile 2", "Profile 3"]


def _default_state():
    return {
        "keyboard_path": None,
        "keyboard_name": None,
        "mouse_path": None,
        "mouse_name": None,
        "active_profile": "profile_1",
        "autosave": False,
    }


def ensure_config_exists():
    """Create config dir + shared macros.json + 3 default empty profiles
    if nothing exists yet. Safe to call every startup -- never
    overwrites existing files."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(_default_state(), indent=2))

    if not MACROS_FILE.exists():
        MACROS_FILE.write_text(json.dumps({"macros": []}, indent=2))

    if not ALIASES_FILE.exists():
        ALIASES_FILE.write_text(json.dumps({"aliases": {}}, indent=2))

    for i, name in enumerate(DEFAULT_PROFILE_NAMES, start=1):
        path = PROFILES_DIR / f"profile_{i}.json"
        if not path.exists():
            path.write_text(json.dumps({"name": name, "enabled": {}}, indent=2))


def load_state():
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_macros():
    if not MACROS_FILE.exists():
        return {"macros": []}
    return json.loads(MACROS_FILE.read_text())


def save_macros(data):
    MACROS_FILE.write_text(json.dumps(data, indent=2))


def load_aliases():
    if not ALIASES_FILE.exists():
        return {"aliases": {}}
    return json.loads(ALIASES_FILE.read_text())


def save_aliases(data):
    ALIASES_FILE.write_text(json.dumps(data, indent=2))


def load_profile(profile_id):
    path = PROFILES_DIR / f"{profile_id}.json"
    return json.loads(path.read_text())


def save_profile(profile_id, profile):
    path = PROFILES_DIR / f"{profile_id}.json"
    path.write_text(json.dumps(profile, indent=2))


def list_profile_ids():
    """Returns [(profile_id, display_name), ...] sorted by filename."""
    result = []
    for path in sorted(PROFILES_DIR.glob("profile_*.json")):
        try:
            data = json.loads(path.read_text())
            result.append((path.stem, data.get("name", path.stem)))
        except Exception:
            result.append((path.stem, path.stem))
    return result


def delete_profile(profile_id):
    path = PROFILES_DIR / f"{profile_id}.json"
    if path.exists():
        path.unlink()


def list_input_devices():
    print(f"{'PATH':<20} {'NAME'}")
    for path in list_devices():
        dev = InputDevice(path)
        print(f"{dev.path:<20} {dev.name}")


def list_all_devices_with_names():
    """Every readable input device as [(path, name), ...] -- used by the
    GUI's --testing panel, which deliberately shows unused devices too."""
    result = []
    for path in list_devices():
        try:
            result.append((path, InputDevice(path).name))
        except Exception:
            continue
    return result


# Names of the daemon's own uinput output devices (see init_uinput()
# below). Auto-detect/resolve must never pick one of these as an
# *input* source -- it's tempting for find_best_keyboard() in
# particular to grab macro-daemon-virtual-keyboard, since it declares
# every KEY_* code that exists and therefore maxes out the A-Z
# coverage score. Defined up here (rather than after init_uinput(),
# where the device objects actually live) so it's usable by every
# resolution path regardless of call order.
_OUR_VIRTUAL_DEVICE_NAMES = frozenset({
    "macro-daemon-virtual-keyboard",
    "macro-daemon-virtual-mouse",
})


def find_best_keyboard(preferred_name=None):
    """Picks the device most likely to be the main keyboard: whichever
    one covers the most of the standard A-Z range. Doesn't need any
    real input -- reads declared capabilities only, so this works
    unattended at boot and survives device renumbering across reboots.

    preferred_name, if given, wins outright over the capability score
    if any candidate's name matches exactly -- this is what makes a
    previously-confirmed device "sticky" across reboots even when
    plugging/unplugging other hardware shuffles which one currently
    scores highest.

    Returns (path, name) or (None, None).
    """
    alpha_codes = {getattr(e, f"KEY_{chr(c)}") for c in range(ord("A"), ord("Z") + 1)}
    candidates = []  # (path, name, score)
    for path in list_devices():
        try:
            dev = InputDevice(path)
            if dev.name in _OUR_VIRTUAL_DEVICE_NAMES:
                continue  # never treat our own uinput output as a real input source
            caps = set(dev.capabilities().get(e.EV_KEY, []))
        except Exception:
            continue
        score = len(alpha_codes & caps)
        # Require most of the alphabet present -- filters out decoys
        # like consumer-control/system-control sub-devices that only
        # have a handful of keys.
        if score >= 20:
            candidates.append((path, dev.name, score))

    if not candidates:
        return None, None

    if preferred_name:
        for path, name, _score in candidates:
            if name == preferred_name:
                return path, name

    best = max(candidates, key=lambda c: c[2])
    return best[0], best[1]


def find_best_mouse(preferred_name=None):
    """Picks the device most likely to be a pointer. Prefers a real
    relative-motion mouse (BTN_LEFT + REL_X + REL_Y); falls back to
    any device with BTN_LEFT at all, which covers click-only/absolute
    touchpad nodes that never emit REL_X/REL_Y for movement.

    preferred_name wins outright over that scoring if any candidate's
    name matches exactly -- same "sticky" reasoning as the keyboard.

    Returns (path, name) or (None, None).
    """
    candidates = []  # (path, name, has_rel)
    for path in list_devices():
        try:
            dev = InputDevice(path)
            if dev.name in _OUR_VIRTUAL_DEVICE_NAMES:
                continue  # never treat our own uinput output as a real input source
            caps = dev.capabilities()
        except Exception:
            continue
        keys = set(caps.get(e.EV_KEY, []))
        rels = set(caps.get(e.EV_REL, []))
        if e.BTN_LEFT not in keys:
            continue
        has_rel = e.REL_X in rels and e.REL_Y in rels
        candidates.append((path, dev.name, has_rel))

    if not candidates:
        return None, None

    if preferred_name:
        for path, name, _has_rel in candidates:
            if name == preferred_name:
                return path, name

    for path, name, has_rel in candidates:
        if has_rel:
            return path, name  # real relative-motion mouse -- best match, stop here
    return candidates[0][0], candidates[0][1]


def resolve_device(kind, saved_path, saved_name):
    """Three-tier priority for picking a device at startup:
      1. saved_path still exists AND is still the same physical device
         (verified by name, when we know it) -- cheapest, most direct.
      2. A device matching saved_name exists somewhere else (renumbered
         since last boot) -- still "the same device", just relocated.
      3. Fresh capability-based auto-detect -- first run, or the
         previously-used device genuinely isn't present anymore.
    Returns (path, name, how) where how is one of "remembered",
    "renumbered", "auto-detected", or None if nothing was found.

    Refuses to ever return one of our own virtual output devices,
    even if state.json has one saved from a past bad detection (e.g.
    from before this exclusion existed) -- that saved value is treated
    as if it were missing, so this always falls through to a real
    auto-detect instead of getting permanently stuck on ourselves.
    """
    finder = find_best_keyboard if kind == "keyboard" else find_best_mouse

    if saved_name in _OUR_VIRTUAL_DEVICE_NAMES:
        saved_path, saved_name = None, None

    if saved_path and Path(saved_path).exists():
        try:
            current_name = InputDevice(saved_path).name
        except Exception:
            current_name = None
        if (current_name is not None and current_name not in _OUR_VIRTUAL_DEVICE_NAMES
                and (saved_name is None or current_name == saved_name)):
            return saved_path, current_name, "remembered"

    path, name = finder(preferred_name=saved_name)
    if path is None:
        return None, None, None
    how = "renumbered" if (saved_name and name == saved_name) else "auto-detected"
    return path, name, how


# ---------------------------------------------------------------------------
# VIRTUAL OUTPUT DEVICES
# ---------------------------------------------------------------------------
# Built from e.keys (real, kernel-valid key codes) rather than reflecting
# over dir(e) for "KEY_*" names -- the latter also matches non-key
# pseudo-constants (KEY_MAX etc.) that the kernel's UI_SET_KEYBIT ioctl
# rejects with EINVAL.
#
# TWO separate devices, not one combined device. A single virtual device
# that advertises the entire keyboard keymap AND relative-pointer
# capabilities at once can confuse compositor/libinput device
# classification (this is also why the original CachyOS version of this
# daemon used two devices). kd()/ku()/tap() below route to whichever
# device actually owns a given code, so macro code doesn't need to care
# which device it's actually going out on.

def _is_key_name(code):
    """True if this EV_KEY code is a typing key (KEY_*), not a button (BTN_*)."""
    names = e.keys.get(code, "")
    if isinstance(names, str):
        names = (names,)
    return any(n.startswith("KEY_") for n in names)


def _is_button_name(code):
    """True if this EV_KEY code is a button (BTN_*), e.g. mouse clicks."""
    names = e.keys.get(code, "")
    if isinstance(names, str):
        names = (names,)
    return any(n.startswith("BTN_") for n in names)


_KEY_CODES = sorted(c for c in e.keys.keys() if _is_key_name(c))

# Deliberately NOT "every BTN_* code" here. The full BTN_* namespace (108
# codes) also covers gamepad/joystick/tablet-tool buttons -- declaring
# that whole range on one uinput device is exactly the kind of signal
# that trips udev's device classifier into ID_INPUT_JOYSTICK instead of
# ID_INPUT_MOUSE, which silently breaks cursor movement and clicks under
# Wayland compositors even though the daemon is emitting real events.
_MOUSE_BUTTON_CODES = [
    e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
    e.BTN_SIDE, e.BTN_EXTRA, e.BTN_FORWARD, e.BTN_BACK, e.BTN_TASK,
]
_BUTTON_CODE_SET = set(_MOUSE_BUTTON_CODES)

# Created lazily (see init_uinput()) rather than at import time, so that
# `--list` and other non-emitting code paths never need /dev/uinput
# permissions at all.
ui_keyboard = None
ui_mouse = None


def init_uinput():
    global ui_keyboard, ui_mouse
    ui_keyboard = UInput(
        {e.EV_KEY: _KEY_CODES},
        name="macro-daemon-virtual-keyboard",
    )
    ui_mouse = UInput(
        {
            e.EV_KEY: _MOUSE_BUTTON_CODES,
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
        },
        name="macro-daemon-virtual-mouse",
    )


def _device_for_code(code):
    return ui_mouse if code in _BUTTON_CODE_SET else ui_keyboard

# ---------------------------------------------------------------------------
# PRIMITIVES
# ---------------------------------------------------------------------------

# Per-thread so concurrently-running macros never affect each other's
# timing, and reset to 1.0 at the start of every macro run (see
# _fire_once/_loop_until_stopped below) so it never carries over
# between separate triggers of the same macro either.
_speed_local = threading.local()


def _current_speed_multiplier():
    return getattr(_speed_local, "value", 1.0)


# Panic-button infrastructure (see abort_all() near the bottom of this
# file, and PRIMITIVES_NAMESPACE registration is deliberately NOT done
# for this -- it's a daemon-level safety feature triggered by a
# dedicated hotkey, not something macro code calls itself).
_abort_event = threading.Event()


def _check_abort():
    if _abort_event.is_set():
        raise _MacroAborted()


class _MacroAborted(Exception):
    """Raised internally to unwind a macro's execution when the abort
    hotkey fires mid-run. Never meant to escape to the user -- caught
    in _fire_once/_loop_until_stopped and treated as a clean, silent
    stop, not an error."""


# Every code WE (our own virtual devices) currently have held down,
# via kd() below -- tracked explicitly (rather than trusting the
# kernel's own per-device key-state table) so abort_all() knows
# exactly what to release without any ambiguity about which device
# state it's reading. Guarded by _held_lock since multiple macros can
# run kd()/ku() concurrently on their own threads.
_held_lock = threading.Lock()
_synth_held = set()

# ---------------------------------------------------------------------------
# ignore() -- blocking real physical input from reaching anywhere else
# while a macro runs, so your own keypresses/clicks/mouse movement
# can't interfere with what it's doing. The only way to truly do this
# is an exclusive device grab (EVIOCGRAB) -- everything below exists
# to make that safe: best-effort everywhere, and abort_all() always
# force-releases all of it regardless of what state it thinks it's in.
# ---------------------------------------------------------------------------

_ignore_lock = threading.Lock()
_ignore_flags = {"keyboard": False, "mouse_buttons": False, "mouse_movement": False}

_grab_lock = threading.Lock()
_grab_state = {"keyboard": False, "mouse": False}
_watched_devices = {}  # "keyboard"/"mouse" -> InputDevice, set by watch_device()


def _release_held_from(kind):
    """After grabbing a real device, any key/button that was already
    physically down at that instant would otherwise stay stuck-down
    forever from the focused app's perspective -- it never gets an
    "up" event once the real device disappears out from under it (the
    grab happened mid-hold). Compensates by sending a synthetic
    release for exactly those codes through OUR virtual device, which
    every app sees as perfectly ordinary input, indistinguishable from
    a real key-up.

    kind: "keyboard" releases everything in `held` that ISN'T a mouse
    button code; "mouse" releases everything that IS one. Also drops
    those codes from `held` itself, since we've now told the rest of
    the system they're up regardless of whether they're still
    physically pressed.
    """
    with _lock:
        if kind == "keyboard":
            stuck = [c for c in held if c not in _BUTTON_CODE_SET]
        else:
            stuck = [c for c in held if c in _BUTTON_CODE_SET]
        for code in stuck:
            held.discard(code)

    if not stuck:
        return
    out_dev = ui_keyboard if kind == "keyboard" else ui_mouse
    for code in stuck:
        try:
            out_dev.write(e.EV_KEY, code, 0)
            out_dev.syn()
        except Exception:
            pass
    print(f"Released {len(stuck)} stuck {kind} code(s) held at grab time")


def _apply_grab_state():
    """Grabs/ungrabs the real keyboard/mouse device to match the
    current ignore flags. Best-effort throughout: a device that isn't
    being watched yet, or a grab/ungrab call that fails for whatever
    reason, is silently skipped rather than raising -- this must never
    be able to crash the daemon or take a macro down with it."""
    with _ignore_lock:
        kb_needed = _ignore_flags["keyboard"]
        mouse_needed = _ignore_flags["mouse_buttons"] or _ignore_flags["mouse_movement"]

    with _grab_lock:
        kb_dev = _watched_devices.get("keyboard")
        if kb_dev is not None:
            try:
                if kb_needed and not _grab_state["keyboard"]:
                    kb_dev.grab()
                    _grab_state["keyboard"] = True
                    print("Keyboard grabbed (ignore active)")
                    _release_held_from("keyboard")
                elif not kb_needed and _grab_state["keyboard"]:
                    kb_dev.ungrab()
                    _grab_state["keyboard"] = False
                    print("Keyboard released")
            except Exception as exc:
                print(f"Keyboard grab/ungrab failed: {exc}")

        mouse_dev = _watched_devices.get("mouse")
        if mouse_dev is not None:
            try:
                if mouse_needed and not _grab_state["mouse"]:
                    mouse_dev.grab()
                    _grab_state["mouse"] = True
                    print("Mouse grabbed (ignore active)")
                    _release_held_from("mouse")
                elif not mouse_needed and _grab_state["mouse"]:
                    mouse_dev.ungrab()
                    _grab_state["mouse"] = False
                    print("Mouse released")
            except Exception as exc:
                print(f"Mouse grab/ungrab failed: {exc}")


def _toggle_ignore(what):
    with _ignore_lock:
        _ignore_flags[what] = not _ignore_flags[what]
    _apply_grab_state()


def ignore(what):
    """
    Toggles whether real physical input of the given kind is blocked
    from reaching the rest of the system while active, so your own
    keypresses/clicks/mouse movement can't interfere with whatever a
    macro is doing. A TOGGLE, like a light switch -- calling it again
    with the same argument turns it back off (ignore("mouse") twice in
    a row is a no-op overall).

    Global, not per-macro -- unlike speed(), this genuinely needs to
    be: it's about blocking real hardware input system-wide, not
    scoping a rate to this macro's own execution. If two macros toggle
    the same target at the same time, they're sharing one on/off
    switch, same as any other global state -- that's on you to reason
    about, same caveat as macros calling each other while speed() is
    active.

    what:
      "keyboard" -- blocks all real keyboard input EXCEPT the abort
        hotkey, which keeps working regardless. That's not a special
        case here -- Puppetry's own reading of the device never stops
        just because it's grabbed (a grab only blocks OTHER listeners,
        never the process holding it), so abort detection is
        unaffected either way.
      "mouse" -- shorthand for toggling both of the below together.
      "mouse_buttons" -- blocks only real mouse clicks; movement still
        works normally.
      "mouse_movement" -- blocks only real mouse movement; clicks
        still work normally.

    IMPORTANT: this works by actually, exclusively grabbing the real
    device while active -- the only way to truly block input at this
    level, and a bigger deal than anything else in here. If a macro
    crashes or gets stuck without toggling it back off, physical input
    could stay blocked. The abort hotkey ALWAYS force-releases every
    active ignore as part of what it does, specifically as a safety
    net for exactly that -- and the checkbox-driven version of this in
    the editor wraps its macro in a try/finally, so a raised exception
    still restores it correctly even without hitting abort.
    """
    if what == "mouse":
        _toggle_ignore("mouse_buttons")
        _toggle_ignore("mouse_movement")
        return
    if what not in _ignore_flags:
        raise ValueError(f"ignore: unknown target {what!r} -- expected "
                          "'keyboard', 'mouse', 'mouse_buttons', or 'mouse_movement'")
    _toggle_ignore(what)


def speed(multiplier):
    """
    Multiplies every wait/hold/movement DURATION from this point on,
    for the rest of THIS macro's execution (including any macros it
    calls along the way) -- not the whole daemon, and not other macros
    running independently on their own threads. Call it again to
    change the rate further, as many times as you like in one macro --
    e.g. speed up just one section and bring it back after:

        speed(3)
        move_mouse(500, 0)   # takes 1/3 the normal time
        speed(1)
        move_mouse(0, 500)   # back to normal speed

    Affects: wait(), tap()'s hold time, move_mouse()'s time_, and
    type()'s time_per_letter (via tap(), which it's built on). Does
    NOT affect anything with no duration to begin with (kd(), ku()).
    Resets to 1 automatically at the start of every run of this macro
    -- it never carries over between separate triggers, or between
    different macros running concurrently on their own threads.
    """
    _speed_local.value = multiplier


def _resolve(key):
    """Accept either a bare ecodes int (KEY_A) or a string name ("KEY_A")."""
    if isinstance(key, str):
        return getattr(e, key)
    return key


def kd(key):
    """Key down -- works for both typing keys and mouse buttons, routed
    to whichever virtual device actually owns that code. Pair with
    ku() -- does not auto-release."""
    code = _resolve(key)
    dev = _device_for_code(code)
    dev.write(e.EV_KEY, code, 1)
    dev.syn()
    with _held_lock:
        _synth_held.add(code)


def ku(key):
    """Key up. Same auto-routing as kd()."""
    code = _resolve(key)
    dev = _device_for_code(code)
    dev.write(e.EV_KEY, code, 0)
    dev.syn()
    with _held_lock:
        _synth_held.discard(code)


def tap(key, time_=0.1):
    """Down, wait `time_` seconds, up. Default hold is 0.1s. Works for
    both typing keys and mouse buttons. Hold time is scaled by
    speed(), like everything else with a duration (via wait(), which
    this is built on -- see wait() for the abort/speed behavior that
    gives this)."""
    kd(key)
    wait(time_)
    ku(key)


def _move_rel_step(dx, dy):
    if dx:
        ui_mouse.write(e.EV_REL, e.REL_X, dx)
    if dy:
        ui_mouse.write(e.EV_REL, e.REL_Y, dy)
    ui_mouse.syn()


def _ease(t, style):
    if style == "linear":
        return t
    if style == "in":
        return t * t
    if style == "out":
        return 1 - (1 - t) * (1 - t)
    # "inout" (default) and any unrecognized value fall back to this --
    # standard easeInOutQuad. (Previously used smoothstep, which
    # decelerates to zero velocity at both ends and can feel too weak.)
    if t < 0.5:
        return 2 * t * t
    return 1 - ((-2 * t + 2) ** 2) / 2


def _get_cursor_pos_kde(timeout=1.0):
    """
    Asks KWin where the cursor currently is, via the `kdotool` CLI.

    There's no portable way to read the cursor position on a Wayland/
    KWin session -- Wayland deliberately doesn't expose it to arbitrary
    clients. kdotool works around this the same way every other KWin
    automation tool does: it loads a throwaway KWin script (which DOES
    have access to workspace.cursorPos) over KWin's own D-Bus scripting
    interface, runs it, and prints the result. That's what's actually
    happening under this subprocess call.

    Returns (x, y) as ints in absolute screen/layout coordinates, or
    None if kdotool isn't installed, KWin didn't respond in time, or
    the output couldn't be parsed. Callers treat None as "position
    unknown" and fall back accordingly -- this is expected to fail
    sometimes (e.g. mid-login, or if kdotool isn't on PATH) and should
    never raise.
    """
    try:
        result = subprocess.run(
            ["kdotool", "getmouselocation", "--shell"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    x = y = None
    for line in result.stdout.splitlines():
        if line.startswith("X="):
            x = int(line[2:])
        elif line.startswith("Y="):
            y = int(line[2:])
    if x is None or y is None:
        return None
    return (x, y)


def move_mouse(x_pixels, y_pixels, time_=0.25, easing="inout", async_=False, move_to=False):
    """
    move_to=False (default): move BY (x_pixels, y_pixels) -- relative
    to wherever the cursor currently is, same behavior as always.

    move_to=True: move TO the absolute screen coordinates
    (x_pixels, y_pixels) instead. This asks KWin where the cursor
    actually is right now, computes the difference between that and
    the target, and performs exactly that relative move -- same
    easing/timing as the relative case. It does NOT jump/snap to the
    target and does NOT detour through a corner as its normal path.

    After that move, it re-checks the actual landing position and, if
    it's off, fires small corrective nudges (up to a few) until it's
    exact. This matters in practice: libinput applies velocity-based
    pointer acceleration to relative motion, so a fast multi-step eased
    move can land a bit short or long of the literal delta requested --
    worse over long distances and with easing curves that pack more
    distance into fewer/faster steps (e.g. "inout"'s middle section).
    The correction pass makes the final position exact regardless of
    that curve, without needing to know or configure it.

    If the current position can't be determined (kdotool missing,
    KWin not responding, timeout, etc.), move_to falls back to a
    corner-anchored move instead of guessing: it snaps instantly to
    the top-left corner (0, 0) -- reliable because a huge relative
    move always clamps to the screen edge, no query needed -- and then
    performs the normal eased move using (x_pixels, y_pixels) as the
    offset from that known origin. So the fallback still lands
    correctly, just always via the corner rather than the shortest
    path -- and it skips the correction pass, since there was no
    reliable position to correct against in the first place.

    easing: "none" (instant jump, no interpolation), "linear" (constant
    speed), "in" (slow start, accelerating), "out" (fast start,
    decelerating), or "inout" (default -- eases both ends, symmetric).
    async_: False (default) -- blocks until the movement finishes.
    True -- starts the movement on a background thread and returns
    immediately, so the rest of the macro keeps running while the
    mouse is still moving. (Named async_ with a trailing underscore
    because "async" is a reserved keyword in Python.)

    time_ is scaled by speed(), like everything else with a duration --
    note that easing="none" already ignores time_ entirely (see below),
    so speed() has nothing to scale on an instant move either.
    """

    def _do_move():
        scaled_time = time_ * _current_speed_multiplier()
        dx, dy = x_pixels, y_pixels
        had_position = False

        if move_to:
            pos = _get_cursor_pos_kde()
            if pos is not None:
                had_position = True
                dx, dy = x_pixels - pos[0], y_pixels - pos[1]
            else:
                # Unknown position -- clamp to the corner instantly,
                # then treat (x_pixels, y_pixels) as the offset from
                # that known (0, 0) origin for the eased move below.
                _move_rel_step(-100000, -100000)

        if easing == "none" or scaled_time <= 0:
            _move_rel_step(dx, dy)
        else:
            steps = max(1, int(scaled_time * 120))  # ~120 steps/sec, smooth without flooding uinput
            prev = 0.0
            for i in range(1, steps + 1):
                _check_abort()  # long/slow moves stay responsive to the panic-button hotkey
                t = i / steps
                cur = _ease(t, easing)
                _move_rel_step(round(dx * (cur - prev)), round(dy * (cur - prev)))
                prev = cur
                time.sleep(scaled_time / steps)

        if move_to and had_position:
            # Pointer acceleration can leave us a bit off target --
            # close the loop instead of trusting the math above blindly.
            for _ in range(3):
                pos = _get_cursor_pos_kde()
                if pos is None:
                    break
                err_x, err_y = x_pixels - pos[0], y_pixels - pos[1]
                if err_x == 0 and err_y == 0:
                    break
                _move_rel_step(err_x, err_y)

    if async_:
        threading.Thread(target=_do_move, daemon=True).start()
    else:
        _do_move()


def wheel(amount):
    ui_mouse.write(e.EV_REL, e.REL_WHEEL, amount)
    ui_mouse.syn()


def wait(time_, precise=False):
    """
    Wait `time_` seconds (scaled by speed(), like everything else with
    a duration).
    precise=False (default): time.sleep() -- cheap, accurate enough for
      almost everything (Linux's sleep is backed by clock_nanosleep).
    precise=True: busy-waits against time.perf_counter() instead. Costs
      real CPU for the duration, only worth it for timing that actually
      needs sub-millisecond accuracy.

    Interruptible by the abort hotkey either way -- checked every 30ms
    (non-precise) or continuously (precise), so a long wait() never
    blocks a panic-button abort for more than a beat.
    """
    time_ = time_ * _current_speed_multiplier()
    if not precise:
        remaining = time_
        chunk = 0.03
        while remaining > 0:
            _check_abort()
            this_chunk = chunk if remaining > chunk else remaining
            time.sleep(this_chunk)
            remaining -= this_chunk
        _check_abort()
        return
    start = time.perf_counter()
    while (time.perf_counter() - start) < time_:
        _check_abort()


# Character -> (KEY_* name, needs_shift) for type(). US QWERTY layout --
# this maps to physical key positions, so it assumes that's the active
# keyboard layout regardless of what layout your OS is configured with.
_CHAR_TO_KEY = {}
for _i in range(26):
    _letter = chr(ord("a") + _i)
    _CHAR_TO_KEY[_letter] = (f"KEY_{_letter.upper()}", False)
    _CHAR_TO_KEY[_letter.upper()] = (f"KEY_{_letter.upper()}", True)
_SHIFT_DIGIT_SYMBOLS = "!@#$%^&*()"
for _i in range(10):
    _CHAR_TO_KEY[str(_i)] = (f"KEY_{_i}", False)
    _CHAR_TO_KEY[_SHIFT_DIGIT_SYMBOLS[_i]] = (f"KEY_{_i}", True)
_CHAR_TO_KEY.update({
    " ": ("KEY_SPACE", False), "\n": ("KEY_ENTER", False), "\t": ("KEY_TAB", False),
    ".": ("KEY_DOT", False), ">": ("KEY_DOT", True),
    ",": ("KEY_COMMA", False), "<": ("KEY_COMMA", True),
    "/": ("KEY_SLASH", False), "?": ("KEY_SLASH", True),
    ";": ("KEY_SEMICOLON", False), ":": ("KEY_SEMICOLON", True),
    "'": ("KEY_APOSTROPHE", False), '"': ("KEY_APOSTROPHE", True),
    "-": ("KEY_MINUS", False), "_": ("KEY_MINUS", True),
    "=": ("KEY_EQUAL", False), "+": ("KEY_EQUAL", True),
    "[": ("KEY_LEFTBRACE", False), "{": ("KEY_LEFTBRACE", True),
    "]": ("KEY_RIGHTBRACE", False), "}": ("KEY_RIGHTBRACE", True),
    "\\": ("KEY_BACKSLASH", False), "|": ("KEY_BACKSLASH", True),
    "`": ("KEY_GRAVE", False), "~": ("KEY_GRAVE", True),
})


def type_text(text, time_per_letter=0.05, async_=False):
    """
    Types `text` out character by character (US QWERTY layout), holding
    each key for `time_per_letter` seconds. Unsupported characters are
    silently skipped. Exposed to macro code as "type" (shadows Python's
    builtin type() only within macro code's own namespace, not
    anywhere else).
    async_: False (default) -- blocks until typing finishes. True --
    types on a background thread and returns immediately.
    """

    def _do_type():
        for ch in text:
            entry = _CHAR_TO_KEY.get(ch)
            if entry is None:
                continue
            key_name, needs_shift = entry
            code = getattr(e, key_name, None)
            if code is None:
                continue
            if needs_shift:
                kd(e.KEY_LEFTSHIFT)
            tap(code, time_per_letter)
            if needs_shift:
                ku(e.KEY_LEFTSHIFT)

    if async_:
        threading.Thread(target=_do_type, daemon=True).start()
    else:
        _do_type()


# Bare KEY_*/BTN_* names available to macro code without an "e." prefix.
_KEY_CONSTANTS = {name: getattr(e, name) for name in dir(e) if name.startswith(("KEY_", "BTN_"))}

PRIMITIVES_NAMESPACE = {
    "kd": kd,
    "ku": ku,
    "tap": tap,
    "type": type_text,
    "move_mouse": move_mouse,
    "wheel": wheel,
    "wait": wait,
    "speed": speed,
    "ignore": ignore,
    "time": time,
    **_KEY_CONSTANTS,
}

# ---------------------------------------------------------------------------
# SIMPLIFIED NAMES (opt-in per macro via "simplified_names": true)
# ---------------------------------------------------------------------------
# Lets macro code write tap(a) instead of tap(KEY_A). Where a short name
# is genuinely ambiguous (mouse click vs. arrow key both wanting "left"),
# the arrow key keeps the plain word and the mouse button gets a short
# acronym instead (LMB/RMB/MMB) -- arrows are the more natural claim on
# "left"/"right", so the button is the one asked to compromise.

SIMPLIFIED_NAMES = {}
for _i in range(26):
    _letter = chr(ord("A") + _i)
    SIMPLIFIED_NAMES[_letter] = f"KEY_{_letter}"
    SIMPLIFIED_NAMES[_letter.lower()] = f"KEY_{_letter}"
for _d in range(10):
    SIMPLIFIED_NAMES[f"D{_d}"] = f"KEY_{_d}"
    SIMPLIFIED_NAMES[f"d{_d}"] = f"KEY_{_d}"
for _i in range(1, 13):
    SIMPLIFIED_NAMES[f"F{_i}"] = f"KEY_F{_i}"
SIMPLIFIED_NAMES.update({
    "SPACE": "KEY_SPACE", "ENTER": "KEY_ENTER", "ESC": "KEY_ESC", "TAB": "KEY_TAB",
    "BACKSPACE": "KEY_BACKSPACE", "DEL": "KEY_DELETE", "DELETE": "KEY_DELETE",
    "CAPSLOCK": "KEY_CAPSLOCK",
    "SHIFT": "KEY_LEFTSHIFT", "RSHIFT": "KEY_RIGHTSHIFT",
    "CTRL": "KEY_LEFTCTRL", "RCTRL": "KEY_RIGHTCTRL",
    "ALT": "KEY_LEFTALT", "RALT": "KEY_RIGHTALT",
    "META": "KEY_LEFTMETA", "WIN": "KEY_LEFTMETA", "SUPER": "KEY_LEFTMETA",
    "RMETA": "KEY_RIGHTMETA", "RWIN": "KEY_RIGHTMETA",
    "UP": "KEY_UP", "DOWN": "KEY_DOWN", "LEFT": "KEY_LEFT", "RIGHT": "KEY_RIGHT",
    "HOME": "KEY_HOME", "END": "KEY_END", "PAGEUP": "KEY_PAGEUP", "PAGEDOWN": "KEY_PAGEDOWN",
    "INSERT": "KEY_INSERT",
    "LMB": "BTN_LEFT", "RMB": "BTN_RIGHT", "MMB": "BTN_MIDDLE",
    "MB4": "BTN_SIDE", "MB5": "BTN_EXTRA",
})
# Case-insensitive: every name above also gets a lowercase alias
# (lmb, space, ctrl, f1, ... all resolve the same as their uppercase
# form). Single letters and digits already got both cases above.
for _simple in list(SIMPLIFIED_NAMES.keys()):
    SIMPLIFIED_NAMES[_simple.lower()] = SIMPLIFIED_NAMES[_simple]


def _build_simplified_namespace():
    """Built-in simplified names resolved to real codes, with any custom
    user aliases (aliases.json) layered on top -- a custom alias's target
    can be either a built-in simplified name or a raw KEY_*/BTN_* name."""
    ns = {}
    for simple_name, real_name in SIMPLIFIED_NAMES.items():
        code = getattr(e, real_name, None)
        if code is not None:
            ns[simple_name] = code

    aliases = load_aliases().get("aliases", {})
    for custom_name, target in aliases.items():
        real_name = SIMPLIFIED_NAMES.get(target, target)
        code = getattr(e, real_name, None)
        if code is not None:
            ns[custom_name] = code
    return ns

# ---------------------------------------------------------------------------
# COMPILING MACRO BODY TEXT INTO CALLABLE FUNCTIONS
# ---------------------------------------------------------------------------

def sanitize_macro_name(name):
    """Turns a macro's display name into a valid Python identifier so it
    can be called from other macros' code, e.g. "Flick and Click" ->
    "Flick_and_Click". Spaces/dashes/punctuation all become underscores."""
    ident = re.sub(r"\W", "_", name or "macro")
    if not ident or ident[0].isdigit():
        ident = f"m_{ident}"
    return ident


def compile_macro(macro_def, macro_refs=None):
    """
    Wraps the raw body text stored in macro_def["code"] into a real
    function and returns it as a zero-arg callable. Raises SyntaxError
    (or whatever the body itself raises at compile time) if the code is
    invalid -- the caller decides whether to skip that macro or abort.

    macro_refs: optional {sanitized_name: callable} for every OTHER
    macro, letting this macro's code call them by name directly (e.g.
    "Flick and Click" becomes callable as Flick_and_Click()). These are
    currently zero-argument calls -- any args you pass are accepted
    but ignored, since macros don't have declared parameters yet.
    Nothing stops two macros from calling each other and recursing
    forever -- that's on you to avoid, it isn't detected here.

    macro_def["ignore_keyboard"/"ignore_mouse_buttons"/"ignore_mouse_movement"]:
    the editor's "Ignore ..." checkboxes. When set, this wraps the
    compiled body in a try/finally that calls ignore(target) on entry
    and ignore(target) again (toggling it back off) on exit -- so it's
    just sugar for calling ignore() by hand at the top of your code,
    except it's guaranteed to toggle back off even if the macro raises
    partway through. (The abort hotkey is a second, independent safety
    net on top of that -- see ignore()'s own docstring.)
    """
    body = macro_def.get("code", "") or "pass"

    ignore_targets = [target for target, key in (
        ("keyboard", "ignore_keyboard"),
        ("mouse_buttons", "ignore_mouse_buttons"),
        ("mouse_movement", "ignore_mouse_movement"),
    ) if macro_def.get(key)]

    if ignore_targets:
        on_block = "\n".join(f"    ignore({target!r})" for target in ignore_targets)
        off_block = "\n".join(f"        ignore({target!r})" for target in ignore_targets)
        body_indented = textwrap.indent(body, "        ")
        src = (
            f"def _macro(*_args, **_kwargs):\n"
            f"{on_block}\n"
            f"    try:\n"
            f"{body_indented}\n"
            f"    finally:\n"
            f"{off_block}\n"
        )
    else:
        body_indented = textwrap.indent(body, "    ")
        src = f"def _macro(*_args, **_kwargs):\n{body_indented}\n"

    namespace = dict(PRIMITIVES_NAMESPACE)
    if macro_def.get("simplified_names"):
        namespace.update(_build_simplified_namespace())
    if macro_refs:
        namespace.update(macro_refs)

    local_ns = {}
    compiled = compile(src, filename=f"<macro:{macro_def.get('name', macro_def.get('id'))}>", mode="exec")
    exec(compiled, namespace, local_ns)
    return local_ns["_macro"]


# ---------------------------------------------------------------------------
# RUNTIME MACRO STATE (per-macro thread + stop control for hold/toggle)
# ---------------------------------------------------------------------------

class RunningMacro:
    __slots__ = ("thread", "stop_event", "active_hold")

    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.active_hold = False  # only meaningful for repeat_mode == "hold"


class Macro:
    __slots__ = ("id", "name", "enabled", "repeat_mode", "combo", "func", "runtime")

    def __init__(self, macro_def, enabled, macro_refs=None):
        self.id = macro_def.get("id") or str(uuid.uuid4())
        self.name = macro_def.get("name", self.id)
        self.enabled = bool(enabled)
        self.repeat_mode = macro_def.get("repeat_mode", "none")
        self.combo = frozenset(_resolve(k) for k in macro_def.get("combo", []))
        self.func = compile_macro(macro_def, macro_refs=macro_refs)
        self.runtime = RunningMacro()


def _fire_once(macro):
    def _run():
        _speed_local.value = 1.0  # fresh 1x for every top-level trigger
        try:
            macro.func()
        except _MacroAborted:
            pass  # panic-button abort -- clean stop, not an error
    threading.Thread(target=_run, daemon=True).start()


def _loop_until_stopped(macro):
    """Runs the macro body repeatedly. Checks the ordinary per-macro
    stop flag (combo released, toggled off) BETWEEN iterations only --
    so a running iteration always finishes naturally, and no new one
    starts once stopped. The global abort hotkey is the one exception:
    it can interrupt mid-iteration too (see _check_abort() calls inside
    wait()/move_mouse()), since it's specifically meant as an emergency
    stop, not a graceful one."""
    rt = macro.runtime
    try:
        while not rt.stop_event.is_set():
            _speed_local.value = 1.0  # fresh 1x every iteration -- a
            # speed() call left active at the end of one loop pass must
            # not silently carry into the next.
            macro.func()
    except _MacroAborted:
        pass  # panic-button abort -- clean stop, not an error
    rt.active_hold = False


def _start_loop(macro):
    rt = macro.runtime
    rt.stop_event.clear()
    rt.active_hold = True
    rt.thread = threading.Thread(target=_loop_until_stopped, args=(macro,), daemon=True)
    rt.thread.start()


def _stop_loop(macro):
    macro.runtime.stop_event.set()


def _is_looping(macro):
    return macro.runtime.thread is not None and macro.runtime.thread.is_alive()


def abort_all():
    """Panic button. Immediately stops every currently-running macro --
    both mid-execution (via the cooperative _check_abort() points
    inside wait()/tap()/move_mouse()) and any hold/toggle loop that
    would otherwise start another iteration -- releases every key/
    button WE (Puppetry's own virtual devices) currently have held
    down, and force-releases any active ignore()/grab, regardless of
    what state it thinks it's in.

    Never INJECTS anything on the real keyboard/mouse: Puppetry only
    ever reads those. The one thing it CAN touch is an exclusive grab
    a macro's ignore() may have taken to block real input -- releasing
    that just restores normal function, it doesn't inject anything
    either. Between the held-key release and the grab release, this
    covers both ways a crashed/stuck macro could leave real input in a
    bad state without a full reboot.
    """
    _abort_event.set()
    for macro in MACROS:
        macro.runtime.stop_event.set()

    # Grabs get released immediately, not on the delayed timer below --
    # a stuck grab means the user can't type/click AT ALL, which is
    # more urgent to fix than the held-key cleanup, and releasing one
    # is always safe regardless of whether any macro thread has
    # actually finished unwinding yet.
    with _ignore_lock:
        for key in _ignore_flags:
            _ignore_flags[key] = False
    with _grab_lock:
        for kind, dev in _watched_devices.items():
            if _grab_state.get(kind):
                try:
                    dev.ungrab()
                    print(f"{kind.capitalize()} force-released (abort)")
                except Exception as exc:
                    print(f"{kind.capitalize()} force-release failed: {exc}")
                _grab_state[kind] = False

    def _release_and_clear():
        # Give any macro thread that was mid-flight (or spawned in the
        # same instant as the abort) a fair window to actually reach a
        # _check_abort() point and unwind, before we clear the flag --
        # otherwise a macro whose thread starts a beat late could miss
        # the pulse entirely and keep running.
        time.sleep(0.3)
        with _held_lock:
            stuck = list(_synth_held)
            _synth_held.clear()
        for code in stuck:
            dev = _device_for_code(code)
            dev.write(e.EV_KEY, code, 0)
            dev.syn()
        _abort_event.clear()

    threading.Thread(target=_release_and_clear, daemon=True).start()


# ---------------------------------------------------------------------------
# EVENT LOOP
# ---------------------------------------------------------------------------

held = set()
_lock = threading.Lock()
MACROS = []  # populated at startup from the active profile
ABORT_CODE = e.KEY_PAUSE  # overwritten from state.json's "abort_key" in main()


def _handle_key_event(code, value):
    if value == 1 and code == ABORT_CODE:
        # Dedicated panic-button hotkey -- never participates in combo
        # matching at all (not even added to `held`), so it can't be
        # accidentally folded into some other macro's combo.
        abort_all()
        return

    if value == 1:  # fresh key down (not autorepeat)
        with _lock:
            held.add(code)
            current = frozenset(held)

        for macro in MACROS:
            if not macro.enabled or code not in macro.combo or not (macro.combo <= current):
                continue

            if macro.repeat_mode == "none":
                _fire_once(macro)

            elif macro.repeat_mode == "hold":
                if not _is_looping(macro):
                    _start_loop(macro)
                # if already looping, a repeated combo-complete press
                # (e.g. from key repeat semantics elsewhere) is a no-op

            elif macro.repeat_mode == "toggle":
                if _is_looping(macro):
                    _stop_loop(macro)
                else:
                    _start_loop(macro)

    elif value == 0:  # key up
        with _lock:
            held.discard(code)
            current = frozenset(held)

        # For "hold" macros: if this release breaks a combo that was
        # actively looping, signal stop (current iteration still
        # finishes naturally inside _loop_until_stopped).
        for macro in MACROS:
            if macro.repeat_mode == "hold" and macro.runtime.active_hold:
                if code in macro.combo and not (macro.combo <= current):
                    _stop_loop(macro)

    # value == 2 (autorepeat) ignored


async def watch_device(path, kind):
    """kind: "keyboard" or "mouse" -- identifies which device this is
    for combo-matching AND for the ignore()/grab machinery above.
    Registers itself in _watched_devices so ignore() can find it.

    Normally (not grabbed) this only reads for combo-matching -- the
    real events already reach the rest of the system natively, nothing
    to forward. Once grabbed (some ignore flag is active), NOTHING
    else can see this device's events anymore, so this loop also
    becomes responsible for forwarding through whichever categories
    ISN'T currently being ignored -- e.g. with only mouse_buttons
    ignored, real movement still needs to reach the cursor normally,
    which now only happens because this loop re-emits it onto our own
    virtual mouse device.
    """
    dev = InputDevice(path)
    _watched_devices[kind] = dev
    print(f"Watching {dev.path} ({dev.name}) -- read-only, not grabbed")
    async for ev in dev.async_read_loop():
        if ev.type == e.EV_KEY:
            _handle_key_event(ev.code, ev.value)
            if kind == "mouse" and _grab_state.get("mouse") and not _ignore_flags["mouse_buttons"]:
                try:
                    ui_mouse.write(e.EV_KEY, ev.code, ev.value)
                    ui_mouse.syn()
                except Exception:
                    pass
        elif ev.type == e.EV_REL and kind == "mouse" and _grab_state.get("mouse") \
                and not _ignore_flags["mouse_movement"]:
            try:
                ui_mouse.write(e.EV_REL, ev.code, ev.value)
                ui_mouse.syn()
            except Exception:
                pass


async def main():
    ensure_config_exists()
    state = load_state()

    global ABORT_CODE
    ABORT_CODE = getattr(e, state.get("abort_key") or "KEY_PAUSE", e.KEY_PAUSE)

    # Resolve real input devices BEFORE creating our own virtual
    # output devices below -- otherwise our virtual keyboard/mouse
    # exist on /dev/input during auto-detect and can get mistaken for
    # the real thing (see _OUR_VIRTUAL_DEVICE_NAMES). The name-based
    # exclusion in find_best_keyboard/find_best_mouse/resolve_device
    # already guards against that directly, but keeping this ordering
    # too means the virtual devices are never even candidates.
    keyboard_path, keyboard_name, kb_how = resolve_device(
        "keyboard", state.get("keyboard_path"), state.get("keyboard_name"))
    mouse_path, mouse_name, mouse_how = resolve_device(
        "mouse", state.get("mouse_path"), state.get("mouse_name"))

    init_uinput()

    if keyboard_path:
        print(f"Keyboard: {keyboard_path} ({keyboard_name}) [{kb_how}]")
    if mouse_path:
        print(f"Mouse: {mouse_path} ({mouse_name}) [{mouse_how}]")
    print(f"Abort hotkey: {state.get('abort_key') or 'KEY_PAUSE'}")

    if not keyboard_path or not mouse_path:
        print("Could not determine keyboard/mouse device, automatically "
              f"or from {STATE_FILE} -- set them manually via the GTK "
              "app's Detect buttons, or state.json directly.")
        sys.exit(1)

    # Persist whatever we resolved to, so next boot's name-priority
    # matching has the best possible chance of finding the same device
    # again even if it renumbers.
    state["keyboard_path"] = keyboard_path
    state["keyboard_name"] = keyboard_name
    state["mouse_path"] = mouse_path
    state["mouse_name"] = mouse_name
    save_state(state)

    profile = load_profile(state["active_profile"])
    print(f"Loaded profile: {profile.get('name', state['active_profile'])}")
    enabled_map = profile.get("enabled", {})

    macros_data = load_macros()
    macro_defs = macros_data.get("macros", [])

    # Pass 1: compile every macro once, without cross-references, just
    # to build a {sanitized_name: callable} map other macros can call
    # into. (Two-pass rather than trying to thread a mutable shared
    # namespace through -- simpler and avoids compile-order edge cases.)
    macro_refs = {}
    for macro_def in macro_defs:
        try:
            macro_refs[sanitize_macro_name(macro_def.get("name"))] = compile_macro(macro_def)
        except Exception:
            pass  # real errors get reported for real in pass 2 below

    # Pass 2: compile for real, this time with every other macro
    # callable by name from within each macro's code.
    global MACROS
    MACROS = []
    for macro_def in macro_defs:
        try:
            mid = macro_def.get("id")
            MACROS.append(Macro(macro_def, enabled_map.get(mid, False), macro_refs=macro_refs))
        except Exception as exc:
            # A bad macro shouldn't take down the whole daemon.
            print(f"Skipping macro {macro_def.get('name', macro_def.get('id'))!r}: {exc}")

    await asyncio.gather(
        watch_device(keyboard_path, "keyboard"),
        watch_device(mouse_path, "mouse"),
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        list_input_devices()
        sys.exit(0)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        if ui_keyboard is not None:
            ui_keyboard.close()
        if ui_mouse is not None:
            ui_mouse.close()
