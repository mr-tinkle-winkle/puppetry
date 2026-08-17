#!/usr/bin/env python3
"""
macro_gui.py — GTK4 front-end for the macro daemon config.

Edits ~/.config/macro-daemon/{state.json,profiles/*.json} -- the exact
same files macro_daemon.py reads. Never talks to the running daemon
directly; "Save" writes config to disk, then runs
`systemctl --user restart macro-daemon.service` so the new config takes
effect immediately.

Requires macro_daemon.py to be importable (same directory by default).
"""

import sys
import json
import uuid
import subprocess
import threading
import selectors
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio, Gdk

sys.path.insert(0, str(Path(__file__).parent))
import macro_daemon as md
from evdev import InputDevice, list_devices, ecodes as e

REPEAT_MODES = ["none", "hold", "toggle"]
REPEAT_MODE_LABELS = ["No Repeat", "Hold", "Toggle"]

# ---------------------------------------------------------------------------
# App-wide text/UI zoom. One shared CssProvider registered against the
# display (so it covers every window, present and future) rather than
# per-window -- the +/- buttons next to Save in both MainWindow and
# MacroEditorWindow drive this same shared state. In-memory only
# (resets to default on relaunch) -- not persisted, matching how this
# was asked for as a quick display adjustment, not a saved preference.
# ---------------------------------------------------------------------------

_ZOOM_MIN_PT = 6
_ZOOM_MAX_PT = 24
_ZOOM_STEP_PT = 1
_zoom_pt = 10  # a reasonable baseline; exact default doesn't need to match
                # the theme's real default since the first +/- click
                # already re-bases everything visibly from here anyway
_zoom_provider = Gtk.CssProvider()
_zoom_registered = False


def _apply_zoom():
    _zoom_provider.load_from_string(f"* {{ font-size: {_zoom_pt}pt; }}")


def _ensure_zoom_registered():
    global _zoom_registered
    if _zoom_registered:
        return
    display = Gdk.Display.get_default()
    Gtk.StyleContext.add_provider_for_display(
        display, _zoom_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _apply_zoom()
    _zoom_registered = True


def zoom_in(_btn=None):
    global _zoom_pt
    _zoom_pt = min(_ZOOM_MAX_PT, _zoom_pt + _ZOOM_STEP_PT)
    _apply_zoom()


def zoom_out(_btn=None):
    global _zoom_pt
    _zoom_pt = max(_ZOOM_MIN_PT, _zoom_pt - _ZOOM_STEP_PT)
    _apply_zoom()


def _make_zoom_buttons():
    """A small +/- box, for placing next to a Save row. Every caller
    gets its own button instances (GTK widgets can't be shared across
    parents) but they all drive the one shared zoom state above."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    minus_btn = Gtk.Button(label="-")
    minus_btn.set_tooltip_text("Smaller text/UI")
    minus_btn.connect("clicked", zoom_out)
    box.append(minus_btn)
    plus_btn = Gtk.Button(label="+")
    plus_btn.set_tooltip_text("Larger text/UI")
    plus_btn.connect("clicked", zoom_in)
    box.append(plus_btn)
    return box

DICTIONARY_TEXT = (
    "tap(key, time_=0.1)\n"
    "  Key/button down, wait, up. Default hold is 0.1s. Works for both\n"
    "  typing keys and mouse buttons (e.g. tap(KEY_A), tap(BTN_LEFT)).\n\n"
    "type(text, time_per_letter=0.05, async_=False)\n"
    "  Types out a string character by character (US QWERTY layout).\n"
    "  Handles shifted symbols automatically. Unsupported characters\n"
    "  are skipped. async_=True: types in the background, returns\n"
    "  immediately instead of blocking the rest of the macro.\n\n"
    "wait(time_, precise=False)\n"
    "  Pause for time_ seconds. precise=True uses a busy-wait for\n"
    "  sub-millisecond accuracy (costs real CPU) instead of a normal sleep.\n\n"
    "speed(multiplier)\n"
    "  Scales every duration from this point on -- wait(), tap()'s hold\n"
    "  time, move_mouse()'s time_, and type()'s per-letter timing --\n"
    "  for the rest of THIS macro (and anything it calls). Call it as\n"
    "  many times as you like to change the rate mid-macro, e.g.\n"
    "  speed(3) before a section you want fast, then speed(1) right\n"
    "  after to bring the rest back to normal. Resets to 1 automatically\n"
    "  at the start of every run -- never carries over between separate\n"
    "  triggers, or between other macros running on their own.\n\n"
    "ignore(target)\n"
    "  Blocks real physical input from reaching anywhere else, so your\n"
    "  own keypresses/clicks/movement can't interfere with what this\n"
    "  macro is doing. A TOGGLE, not a one-way switch -- call it again\n"
    "  with the same target to turn it back off. target is one of\n"
    "  \"keyboard\" (blocks all real keys except the abort hotkey, which\n"
    "  always keeps working), \"mouse_buttons\", \"mouse_movement\", or\n"
    "  \"mouse\" (both mouse ones together). Global, not per-macro, and\n"
    "  works by actually grabbing the real device -- see the \"Ignore ...\"\n"
    "  checkboxes below the code box for the guardrailed version of this\n"
    "  (auto-restores even if the macro raises); the abort hotkey is a\n"
    "  second, independent safety net on top of either.\n\n"
    "move_mouse(x_pixels, y_pixels, time_=0.25, easing=\"inout\", async_=False, move_to=False)\n"
    "  Move the mouse by (x, y) over time_ seconds.\n"
    "  easing: none (instant jump), linear (constant speed),\n"
    "  in (slow start), out (slow end), inout (default, eases both).\n"
    "  async_=True: returns immediately, movement continues in the\n"
    "  background instead of blocking the rest of the macro.\n"
    "  move_to=True: (x, y) is an absolute target instead of an offset\n"
    "  -- moves the cursor TO that screen position (checks where it\n"
    "  currently is via KWin, moves the difference, then corrects any\n"
    "  pointer-acceleration drift so it lands exactly on target). Use\n"
    "  the \"Mouse position\" readout below the combo row to find\n"
    "  coordinates.\n\n"
    "wheel(amount)\n"
    "  Scroll. Positive = up, negative = down.\n\n"
    "kd(key)\n"
    "  Down only -- pair with ku(). Works for keys and mouse buttons.\n\n"
    "ku(key)\n"
    "  Up only -- releases what kd() pressed.\n\n"
    "Every KEY_* and BTN_* name (e.g. KEY_A, KEY_LEFTCTRL, BTN_LEFT) is\n"
    "available directly -- no import or prefix needed.\n\n"
    "Other macros are callable by name (spaces/punctuation become\n"
    "underscores, e.g. a macro named \"Flick and Click\" is called as\n"
    "Flick_and_Click()). These calls are zero-argument for now -- any\n"
    "args you pass are accepted but ignored."
)


def _key_name(code):
    """Best-effort reverse lookup of an ecodes int back to a name string.
    e.keys is the single authoritative {code: name(s)} map covering both
    KEY_* and BTN_* codes."""
    name = e.keys.get(code, str(code))
    if isinstance(name, (list, tuple)):
        name = name[0]
    return name


def _blank_macro():
    return {
        "id": str(uuid.uuid4()),
        "name": "New Macro",
        "description": "",
        "repeat_mode": "none",
        "combo": [],
        "code": "",
        "simplified_names": False,
        "ignore_keyboard": False,
        "ignore_mouse_buttons": False,
        "ignore_mouse_movement": False,
    }


def _simplified_names_reference_text():
    """Built from macro_daemon.SIMPLIFIED_NAMES so this can never drift
    out of sync with what the daemon actually resolves."""
    lines = []
    seen_targets = {}
    for simple, real in md.SIMPLIFIED_NAMES.items():
        seen_targets.setdefault(real, []).append(simple)
    for real, simples in seen_targets.items():
        # Skip the redundant lowercase duplicate for single letters.
        shown = sorted(set(simples), key=lambda s: (s.isupper() is False, s))
        lines.append(f"{' / '.join(shown):<20} -> {real}")
    return "\n".join(lines)


def _prompt_text(parent, title, initial_text, on_confirm):
    """Small transient window with a single text entry + OK/Cancel.
    Calls on_confirm(text) if OK is clicked (Enter also confirms)."""
    win = Gtk.Window(title=title, transient_for=parent, modal=True)
    win.set_default_size(320, -1)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                   margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
    win.set_child(box)

    entry = Gtk.Entry(text=initial_text)
    entry.set_activates_default(True)
    box.append(entry)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.connect("clicked", lambda *_: win.close())
    ok_btn = Gtk.Button(label="OK")
    ok_btn.add_css_class("suggested-action")

    def do_ok(*_a):
        on_confirm(entry.get_text())
        win.close()

    ok_btn.connect("clicked", do_ok)
    win.set_default_widget(ok_btn)
    btn_row.append(cancel_btn)
    btn_row.append(ok_btn)
    box.append(btn_row)

    win.present()


# ---------------------------------------------------------------------------
# Combo recording: listens on both configured devices at once, captures the
# largest set of keys that were ever held down simultaneously during the
# recording window.
# ---------------------------------------------------------------------------

class ComboRecorder:
    """Waits for input, then auto-commits once whatever is currently held
    stops changing for STABLE_SECONDS. This deliberately means a brief
    click (e.g. the click that pressed "Record Combo" itself) doesn't get
    captured -- it releases well before the stability window elapses."""

    STABLE_SECONDS = 3.0

    def __init__(self, keyboard_path, mouse_path, on_update, on_done):
        self.keyboard_path = keyboard_path
        self.mouse_path = mouse_path
        self.on_update = on_update  # (held_codes:set, elapsed_stable:float) -> None
        self.on_done = on_done      # (combo_names:list, status:str) -> None ; status: "ok"|"cancelled"|"no_devices"
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._stop.set()

    def _run(self):
        import time as _time
        held = set()
        last_change = _time.monotonic()
        sel = selectors.DefaultSelector()
        devices = []
        finalized = None
        try:
            for path in (self.keyboard_path, self.mouse_path):
                if not path:
                    continue
                try:
                    dev = InputDevice(path)
                    devices.append(dev)
                    sel.register(dev.fd, selectors.EVENT_READ, dev)
                except Exception:
                    pass

            if not devices:
                GLib.idle_add(self.on_done, [], "no_devices")
                return

            while not self._stop.is_set():
                changed = False
                for key, _ in sel.select(timeout=0.1):
                    dev = key.data
                    try:
                        for ev in dev.read():
                            if ev.type != e.EV_KEY:
                                continue
                            if ev.value == 1 and ev.code not in held:
                                held.add(ev.code)
                                changed = True
                            elif ev.value == 0 and ev.code in held:
                                held.discard(ev.code)
                                changed = True
                    except BlockingIOError:
                        pass

                now = _time.monotonic()
                if changed:
                    last_change = now
                    GLib.idle_add(self.on_update, set(held), 0.0)
                elif held:
                    elapsed = now - last_change
                    GLib.idle_add(self.on_update, set(held), elapsed)
                    if elapsed >= self.STABLE_SECONDS:
                        finalized = set(held)
                        break
        finally:
            for dev in devices:
                try:
                    dev.close()
                except Exception:
                    pass
            if finalized is not None:
                GLib.idle_add(self.on_done, [_key_name(c) for c in finalized], "ok")
            else:
                GLib.idle_add(self.on_done, [], "cancelled")


# ---------------------------------------------------------------------------
# Live transcription: turns real key/click/movement events into macro code
# as you perform them, inserted straight into the code editor.
# ---------------------------------------------------------------------------

class InputTranscriber:
    """Watches your real devices while running and streams generated
    primitive calls (kd/ku/wait/move_mouse) back to the editor as you
    act -- "record a macro by just doing it" instead of hand-writing it.

    Keyboard: every KEY_* press/release becomes a kd()/ku() pair, with
    a preceding wait() line whenever the gap since the last emitted
    event is large enough to matter -- this is what preserves your
    actual pacing on replay.

    Mouse clicks (BTN_*): identical kd()/ku()/wait() treatment,
    regardless of the raw-mouse setting below -- clicks are always
    discrete events; there's no "raw vs waypoint" distinction for them.

    Mouse movement has two modes:
      - Default (raw_mouse=False): movement is NOT continuously
        transcribed. Instead, pressing the configured ping key samples
        wherever the cursor actually is right now (same KWin/kdotool
        query move_to=True uses at runtime) and emits a single
        move_mouse(x, y, move_to=True, time_=<gap since the last
        emitted event>) call -- the elapsed time IS the move's
        duration, so there's no separate wait() line for pings
        specifically (that would double-count the gap: once as a
        wait, again as the move's own duration). Requires KDE Plasma/
        KWin; if the position query fails, that ping is skipped and a
        comment noting the failure is inserted instead of a bogus call.
      - Raw (raw_mouse=True): the physical mouse's motion is sampled
        on a fixed tick rate (raw_hz, default 60 -- independent of the
        mouse's actual polling rate) -- whatever raw relative motion
        arrived within each tick's window becomes one
        move_mouse(dx, dy, time_=0, easing="none") call, preceded by
        an explicit wait() for that tick's real elapsed time. No
        querying needed, so this works fine under Hyprland too. This
        is what "unoptimized/ugly" refers to: still far more, far less
        readable lines than the ping-based waypoints above -- just
        batched to a sane, fixed rate rather than one line per raw
        hardware frame (which for a high-polling-rate mouse would be
        excessive). Higher raw_hz = more/finer lines and closer
        fidelity to the actual motion; lower = fewer/coarser lines.

        set_positions=True changes each tick's line to
        move_mouse(x, y, move_to=True, ...) with the literal absolute
        coordinate instead of a relative (dx, dy) -- tracked by
        querying KWin ONCE at the start of recording and then locally
        accumulating raw deltas on top of that (re-synced against a
        fresh query every couple of seconds to correct any drift),
        rather than querying per tick. That keeps RECORDING cheap.
        PLAYBACK is a different story: move_to=True queries KWin at
        runtime on every call, so a macro recorded this way will
        replay noticeably slower/less smooth than plain raw mode,
        since it's now making up to raw_hz KWin round-trips a second
        instead of zero. Worth it only when you actually want literal,
        editable absolute coordinates in the generated code -- not for
        macros meant to replay your motion smoothly in real time.

        same_start is the lighter-weight alternative to set_positions:
        rather than making every tick absolute, it inserts exactly ONE
        move_mouse(x, y, move_to=True) line at the very start of the
        recording -- wherever the cursor was when you clicked Start
        Transcribing -- and every tick after that stays a normal
        relative (dx, dy) call. Gives a reproducible starting point
        without set_positions' per-call playback cost. Mutually
        exclusive with set_positions in the editor UI (checking one
        unchecks the other) since set_positions' first tick already
        does this implicitly.

    One shared clock spans every event type -- keyboard, clicks,
    pings, and raw movement all interleave into a single chronological
    script, not separate per-stream timelines.
    """

    WAIT_THRESHOLD = 0.02  # gaps below this aren't worth a wait() line
    RESYNC_INTERVAL = 2.0  # how often set_positions re-queries KWin to correct drift

    RAW_TICK_DEFAULT = 1.0 / 60.0  # fallback if raw_hz isn't given/valid

    def __init__(self, keyboard_path, mouse_path, transcribe_keyboard,
                 transcribe_mouse, raw_mouse, set_positions, same_start, raw_hz,
                 ping_code, on_line, on_done):
        self.keyboard_path = keyboard_path
        self.mouse_path = mouse_path
        self.transcribe_keyboard = transcribe_keyboard
        self.transcribe_mouse = transcribe_mouse
        self.raw_mouse = raw_mouse
        self.set_positions = set_positions
        self.same_start = same_start
        self.raw_hz = raw_hz
        self.ping_code = ping_code
        self.on_line = on_line  # (text:str) -> None, GTK thread; text includes trailing \n
        self.on_done = on_done  # (status:str) -> None ; status: "ok"|"no_devices"
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        import time as _time
        sel = selectors.DefaultSelector()
        devices = []
        try:
            wanted_paths = set()
            if (self.transcribe_keyboard or self.transcribe_mouse) and self.keyboard_path:
                wanted_paths.add(self.keyboard_path)  # keyboard also needed for the ping key
            if self.transcribe_mouse and self.mouse_path:
                wanted_paths.add(self.mouse_path)

            if not wanted_paths:
                GLib.idle_add(self.on_done, "no_devices")
                return

            for path in wanted_paths:
                try:
                    dev = InputDevice(path)
                    devices.append(dev)
                    sel.register(dev.fd, selectors.EVENT_READ, dev)
                except Exception:
                    pass

            if not devices:
                GLib.idle_add(self.on_done, "no_devices")
                return

            last_time = _time.monotonic()
            raw_dx, raw_dy = 0, 0
            try:
                hz = float(self.raw_hz)
                RAW_TICK = 1.0 / hz if hz > 0 else self.RAW_TICK_DEFAULT
            except (TypeError, ValueError):
                RAW_TICK = self.RAW_TICK_DEFAULT
            # Fixed sample-rate tick for raw mode, independent of the
            # mouse's actual polling rate -- batches whatever raw motion
            # arrived within each tick's window into a single line,
            # instead of one line per hardware frame (which for a
            # 1000Hz gaming mouse is drastically more granular than
            # useful and was the original source of both the
            # line-count explosion and, combined with the bug below,
            # the "does almost nothing" playback.
            next_raw_tick = _time.monotonic() + RAW_TICK

            # set_positions / same_start both need a starting baseline
            # position -- share the one query rather than doing it
            # twice. set_positions then tracks it continuously via
            # local accumulation (60/sec of actual KWin queries would
            # be far too slow); same_start just emits it once, up
            # front, and every tick after that stays a normal relative
            # call.
            tracked_pos = None
            next_resync = 0.0
            if self.raw_mouse and (self.set_positions or self.same_start):
                baseline = md._get_cursor_pos_kde()
                if baseline is not None:
                    if self.same_start:
                        GLib.idle_add(
                            self.on_line,
                            f"move_mouse({baseline[0]}, {baseline[1]}, move_to=True, time_=0)\n",
                        )
                        # Doesn't count against the first real event's
                        # gap -- this was our own setup, not something
                        # you actually waited through.
                        last_time = _time.monotonic()
                    if self.set_positions:
                        tracked_pos = [baseline[0], baseline[1]]
                        next_resync = _time.monotonic() + self.RESYNC_INTERVAL
                else:
                    GLib.idle_add(
                        self.on_line,
                        "# couldn't read starting cursor position -- "
                        "falling back to relative deltas for this session\n",
                    )

            while not self._stop.is_set():
                for key, _ in sel.select(timeout=0.01):
                    dev = key.data
                    try:
                        for ev in dev.read():
                            if ev.type == e.EV_KEY:
                                code = ev.code

                                if code == self.ping_code:
                                    if ev.value == 1 and self.transcribe_mouse and not self.raw_mouse:
                                        pos = md._get_cursor_pos_kde(timeout=0.5)
                                        now = _time.monotonic()
                                        gap = now - last_time
                                        if pos is None:
                                            text = "# ping failed -- couldn't read cursor position (needs KDE/kdotool)\n"
                                        else:
                                            text = f"move_mouse({pos[0]}, {pos[1]}, move_to=True, time_={gap:.3f})\n"
                                        last_time = now
                                        GLib.idle_add(self.on_line, text)
                                    continue  # ping key (press AND release) never transcribed as a keypress

                                if self.transcribe_keyboard and dev.path == self.keyboard_path \
                                        and _is_key_name(code) and ev.value in (0, 1):
                                    now = _time.monotonic()
                                    gap = now - last_time
                                    line = f"{'kd' if ev.value == 1 else 'ku'}({_key_name(code)})"
                                    text = (f"wait({gap:.3f})\n" if gap >= self.WAIT_THRESHOLD else "") + line + "\n"
                                    last_time = now
                                    GLib.idle_add(self.on_line, text)

                                elif self.transcribe_mouse and dev.path == self.mouse_path \
                                        and _is_button_name(code) and ev.value in (0, 1):
                                    now = _time.monotonic()
                                    gap = now - last_time
                                    line = f"{'kd' if ev.value == 1 else 'ku'}({_key_name(code)})"
                                    text = (f"wait({gap:.3f})\n" if gap >= self.WAIT_THRESHOLD else "") + line + "\n"
                                    last_time = now
                                    GLib.idle_add(self.on_line, text)

                            elif ev.type == e.EV_REL and self.transcribe_mouse and self.raw_mouse \
                                    and dev.path == self.mouse_path:
                                # Just accumulate here -- actual emission
                                # happens on the fixed 60Hz tick below,
                                # not per raw event.
                                if ev.code == e.REL_X:
                                    raw_dx += ev.value
                                elif ev.code == e.REL_Y:
                                    raw_dy += ev.value
                    except BlockingIOError:
                        pass

                if self.transcribe_mouse and self.raw_mouse:
                    now = _time.monotonic()

                    if tracked_pos is not None and now >= next_resync:
                        fresh = md._get_cursor_pos_kde(timeout=0.5)
                        if fresh is not None:
                            tracked_pos[0], tracked_pos[1] = fresh
                        next_resync = now + self.RESYNC_INTERVAL

                    if now >= next_raw_tick:
                        if raw_dx or raw_dy:
                            gap = now - last_time
                            # Unlike keyboard/click events, this ALWAYS
                            # gets an explicit wait() regardless of
                            # WAIT_THRESHOLD -- the whole point of the
                            # fixed tick rate is reproducing that exact
                            # ~16.7ms cadence on playback, and this gap
                            # is routinely right around/below that
                            # threshold, so skipping it here would
                            # silently reintroduce the same
                            # everything-happens-instantly bug this
                            # tick system was built to fix.
                            if tracked_pos is not None:
                                tracked_pos[0] += raw_dx
                                tracked_pos[1] += raw_dy
                                move_line = f"move_mouse({tracked_pos[0]}, {tracked_pos[1]}, move_to=True, time_=0)"
                            else:
                                # time_=0/easing="none" is deliberate:
                                # move_mouse's "none" branch does one
                                # instant relative step and returns
                                # WITHOUT sleeping, ignoring time_
                                # entirely -- so the wait() above is
                                # what actually provides this line's
                                # pacing, not time_.
                                move_line = f'move_mouse({raw_dx}, {raw_dy}, time_=0, easing="none")'
                            text = f"wait({gap:.3f})\n{move_line}\n"
                            last_time = now
                            raw_dx, raw_dy = 0, 0
                            GLib.idle_add(self.on_line, text)
                        next_raw_tick += RAW_TICK
                        if next_raw_tick < now:
                            # Fell behind (e.g. the GTK thread was briefly
                            # busy) -- resync instead of firing a burst
                            # of back-to-back catch-up ticks.
                            next_raw_tick = now + RAW_TICK
        finally:
            for dev in devices:
                try:
                    dev.close()
                except Exception:
                    pass
            GLib.idle_add(self.on_done, "ok")


def detect_ping_key(keyboard_path, mouse_path, on_found, timeout=10.0):
    """Waits for a single KEY_*/BTN_* press on either device, for
    (re)assigning the transcriber's ping trigger. Calls
    on_found(code_or_None, name_or_None) from the GTK main thread."""

    def worker():
        sel = selectors.DefaultSelector()
        devices = []
        found_code = None
        found_name = None
        try:
            for path in (keyboard_path, mouse_path):
                if not path:
                    continue
                try:
                    dev = InputDevice(path)
                    devices.append(dev)
                    sel.register(dev.fd, selectors.EVENT_READ, dev)
                except Exception:
                    pass

            import time as _time
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline and found_code is None:
                for key, _ in sel.select(timeout=0.2):
                    dev = key.data
                    try:
                        for ev in dev.read():
                            if ev.type == e.EV_KEY and ev.value == 1:
                                found_code, found_name = ev.code, _key_name(ev.code)
                                break
                    except BlockingIOError:
                        pass
                    if found_code:
                        break
        finally:
            for dev in devices:
                try:
                    dev.close()
                except Exception:
                    pass
            GLib.idle_add(on_found, found_code, found_name)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Device auto-detect: "press any key on your keyboard" / "move your mouse"
# ---------------------------------------------------------------------------

def _is_key_name(code):
    """True if this EV_KEY code is a typing key (KEY_*), not a button (BTN_*)."""
    names = e.keys.get(code, "")
    if isinstance(names, str):
        names = (names,)
    return any(n.startswith("KEY_") for n in names)


def _is_button_name(code):
    """True if this EV_KEY code is a button (BTN_*), e.g. mouse/touchpad clicks."""
    names = e.keys.get(code, "")
    if isinstance(names, str):
        names = (names,)
    return any(n.startswith("BTN_") for n in names)


def detect_device(kind, on_found, timeout=10.0):
    """kind: 'keyboard' (waits for a KEY_* press) or 'mouse' (waits for
    real relative motion, OR a BTN_* click -- covers click-only/absolute
    touchpad nodes that never emit EV_REL for movement).
    Calls on_found(path_or_None, name_or_None) from the GTK main thread."""

    def worker():
        sel = selectors.DefaultSelector()
        devices = []
        found_path = None
        found_name = None
        try:
            for path in list_devices():
                try:
                    dev = InputDevice(path)
                    if dev.name in md._OUR_VIRTUAL_DEVICE_NAMES:
                        # Never a candidate: if the daemon service is
                        # running and a macro happens to fire mid-detect,
                        # its own synthetic keypress would otherwise be
                        # readable right back off this node and could
                        # get mistaken for a real key/click.
                        dev.close()
                        continue
                    devices.append(dev)
                    sel.register(dev.fd, selectors.EVENT_READ, dev)
                except Exception:
                    pass

            import time as _time
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline and found_path is None:
                for key, _ in sel.select(timeout=0.2):
                    dev = key.data
                    try:
                        for ev in dev.read():
                            if kind == "keyboard" and ev.type == e.EV_KEY and ev.value == 1 \
                                    and _is_key_name(ev.code):
                                found_path, found_name = dev.path, dev.name
                                break
                            if kind == "mouse" and (
                                ev.type == e.EV_REL
                                or (ev.type == e.EV_KEY and ev.value == 1 and _is_button_name(ev.code))
                            ):
                                found_path, found_name = dev.path, dev.name
                                break
                    except BlockingIOError:
                        pass
                    if found_path:
                        break
        finally:
            for dev in devices:
                try:
                    dev.close()
                except Exception:
                    pass
            GLib.idle_add(on_found, found_path, found_name)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Live mouse-position readout (macro editor) -- same KWin/kdotool path
# move_mouse(..., move_to=True) uses at runtime, so what you read here
# is exactly what move_to will use as "current position".
# ---------------------------------------------------------------------------

class MousePositionPoller:
    """Repeatedly asks KWin (via md._get_cursor_pos_kde -- the exact
    same call move_mouse's move_to=True makes) where the cursor is,
    and reports it back on the GTK main thread. Lets you move the
    mouse to a spot and read off the (x, y) to hardcode into a macro,
    instead of trial and error."""

    INTERVAL_SECONDS = 0.25  # ~4Hz -- each poll loads+runs a small KWin
    # script under the hood, so this is deliberately not tighter than that.

    def __init__(self, on_update):
        self.on_update = on_update  # (text:str) -> None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            pos = md._get_cursor_pos_kde(timeout=0.5)
            text = f"{pos[0]}, {pos[1]}" if pos else "unavailable (is kdotool installed?)"
            GLib.idle_add(self.on_update, text)
            self._stop.wait(self.INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Macro editor window
# ---------------------------------------------------------------------------

class MacroEditorWindow(Gtk.Window):
    def __init__(self, app_window, macro, is_new, on_saved):
        super().__init__(title=f"Edit Macro — {macro.get('name', '')}")
        # See MainWindow's identical call for why this is needed
        # separately per-window rather than inherited from the app.
        self.set_icon_name("puppetry")
        self.set_transient_for(app_window)
        self.set_modal(True)
        self.set_default_size(1120, 1280)

        self.app_window = app_window
        self.macro = dict(macro)  # working copy
        self.is_new = is_new
        self.on_saved = on_saved
        self._recorder = None
        self._recording = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=12, margin_bottom=8, margin_start=12, margin_end=12)
        self.set_child(outer)

        # Top toolbar -- stays visible regardless of scroll position in
        # either pane below, since Save/Close and zoom are the kind of
        # controls you want reachable at all times, not buried in a
        # scrollable column.
        toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self.on_save_clicked)
        toolbar_row.append(save_btn)
        save_close_btn = Gtk.Button(label="Save and Close")
        save_close_btn.connect("clicked", self.on_save_and_close_clicked)
        toolbar_row.append(save_close_btn)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda *_: self.close())
        toolbar_row.append(close_btn)
        toolbar_row.append(_make_zoom_buttons())
        outer.append(toolbar_row)

        # Error banner (hidden until needed) -- also full-width, above
        # the pane split, so it's visible no matter which side of the
        # editor you're looking at when a save fails.
        self.error_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.error_label.add_css_class("error")
        outer.append(self.error_label)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        outer.append(paned)

        left_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                margin_top=4, margin_bottom=4, margin_start=4, margin_end=8)
        left_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                            hscrollbar_policy=Gtk.PolicyType.NEVER)
        left_scroller.set_child(left_content)
        paned.set_start_child(left_scroller)

        right_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                 margin_top=4, margin_bottom=4, margin_start=8, margin_end=4,
                                 hexpand=True, vexpand=True)
        paned.set_end_child(right_content)
        paned.set_position(540)  # roughly half of the 1120 default width; draggable either way

        root = left_content  # everything below is unchanged from before except
        # this redirect and the "Code" section further down switching
        # to right_content -- see that comment for why.

        # Name
        root.append(Gtk.Label(label="Name", xalign=0))
        self.name_entry = Gtk.Entry(text=self.macro.get("name", ""))
        root.append(self.name_entry)

        # Description
        root.append(Gtk.Label(label="Description", xalign=0))
        self.desc_entry = Gtk.Entry(text=self.macro.get("description", ""))
        root.append(self.desc_entry)

        # Repeat mode
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(Gtk.Label(label="Repeat mode"))
        self.repeat_dropdown = Gtk.DropDown.new_from_strings(REPEAT_MODE_LABELS)
        current_mode = self.macro.get("repeat_mode", "none")
        idx = REPEAT_MODES.index(current_mode) if current_mode in REPEAT_MODES else 0
        self.repeat_dropdown.set_selected(idx)
        row.append(self.repeat_dropdown)
        root.append(row)

        # Combo
        root.append(Gtk.Label(label="Key combo", xalign=0))
        combo_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.combo_label = Gtk.Label(label=self._combo_display(self.macro.get("combo", [])), xalign=0)
        self.combo_label.set_hexpand(True)
        combo_row.append(self.combo_label)
        self.record_button = Gtk.Button(label="Record Combo")
        self.record_button.connect("clicked", self.on_record_clicked)
        combo_row.append(self.record_button)
        root.append(combo_row)

        # Live mouse position -- for reading off coordinates to use
        # with move_mouse(x, y, move_to=True).
        pos_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pos_row.append(Gtk.Label(label="Mouse position"))
        self.mouse_pos_label = Gtk.Label(label="—", xalign=0)
        self.mouse_pos_label.add_css_class("monospace")
        pos_row.append(self.mouse_pos_label)
        root.append(pos_row)

        self._pos_poller = MousePositionPoller(self._on_mouse_pos_update)
        self._pos_poller.start()
        self.connect("destroy", lambda *_: self._pos_poller.stop())

        # Transcribe Inputs -- records real key/click/movement events
        # as macro code, inserted live at the cursor position in the
        # code box (now over in right_content -- see below).
        root.append(Gtk.Label(label="Transcribe Inputs", xalign=0))

        self._transcriber = None
        self._transcribing = False
        self.ping_code = getattr(e, self.app_window.state.get("transcribe_ping_key") or "KEY_INSERT", e.KEY_INSERT)

        # Transcription settings are app-wide (like the ping key already
        # was), not per-macro -- they're a "how do I want to work right
        # now" preference, not something that makes sense to vary per
        # macro. Restored here every time this editor opens, saved back
        # in on_save_clicked below.
        transcribe_kb_default = self.app_window.state.get("transcribe_keyboard", False)
        transcribe_mouse_default = self.app_window.state.get("transcribe_mouse", False)
        transcribe_raw_default = self.app_window.state.get("transcribe_raw", False)
        transcribe_setpos_default = self.app_window.state.get("transcribe_setpos", False)
        transcribe_samestart_default = self.app_window.state.get("transcribe_samestart", False)
        transcribe_hz_default = self.app_window.state.get("transcribe_raw_hz", 60)

        transcribe_check_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.transcribe_kb_check = Gtk.CheckButton(label="Transcribe Keyboard")
        self.transcribe_kb_check.set_active(transcribe_kb_default)
        transcribe_check_row.append(self.transcribe_kb_check)
        self.transcribe_mouse_check = Gtk.CheckButton(label="Transcribe Mouse")
        self.transcribe_mouse_check.set_active(transcribe_mouse_default)
        transcribe_check_row.append(self.transcribe_mouse_check)
        self.transcribe_raw_check = Gtk.CheckButton(label="Raw Mouse Input")
        self.transcribe_raw_check.set_sensitive(transcribe_mouse_default)
        transcribe_check_row.append(self.transcribe_raw_check)
        root.append(transcribe_check_row)

        # Set Mouse Positions and Same Starting Mouse Position are
        # alternatives, not additive -- set_positions' own first tick
        # already gives an exact starting position, so having both on
        # would just make same_start's line entirely redundant.
        raw_alt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.transcribe_setpos_check = Gtk.CheckButton(label="Set Mouse Positions")
        self.transcribe_setpos_check.set_sensitive(transcribe_mouse_default and transcribe_raw_default)
        raw_alt_row.append(self.transcribe_setpos_check)
        self.transcribe_samestart_check = Gtk.CheckButton(label="Same Starting Mouse Position")
        self.transcribe_samestart_check.set_sensitive(transcribe_mouse_default and transcribe_raw_default)
        raw_alt_row.append(self.transcribe_samestart_check)
        root.append(raw_alt_row)

        raw_freq_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        raw_freq_row.append(Gtk.Label(label="Raw sample rate (Hz)"))
        self.raw_hz_spin = Gtk.SpinButton.new_with_range(1, 1000, 5)
        self.raw_hz_spin.set_value(transcribe_hz_default)
        self.raw_hz_spin.set_sensitive(transcribe_mouse_default and transcribe_raw_default)
        raw_freq_row.append(self.raw_hz_spin)
        root.append(raw_freq_row)

        # Set these AFTER both spin/checkbox defaults are applied above,
        # so restoring a saved raw=True state doesn't get immediately
        # stomped by the checkboxes' own "off by default" toggled logic.
        self.transcribe_raw_check.set_active(transcribe_raw_default)
        self.transcribe_setpos_check.set_active(transcribe_setpos_default)
        self.transcribe_samestart_check.set_active(transcribe_samestart_default and not transcribe_setpos_default)

        def _on_mouse_check_toggled(check_btn):
            self.transcribe_raw_check.set_sensitive(check_btn.get_active())
            if not check_btn.get_active():
                self.transcribe_raw_check.set_active(False)
        self.transcribe_mouse_check.connect("toggled", _on_mouse_check_toggled)

        def _on_raw_check_toggled(check_btn):
            active = check_btn.get_active()
            self.transcribe_setpos_check.set_sensitive(active)
            self.transcribe_samestart_check.set_sensitive(active)
            self.raw_hz_spin.set_sensitive(active)
            if not active:
                self.transcribe_setpos_check.set_active(False)
                self.transcribe_samestart_check.set_active(False)
        self.transcribe_raw_check.connect("toggled", _on_raw_check_toggled)

        def _on_setpos_toggled(check_btn):
            if check_btn.get_active():
                self.transcribe_samestart_check.set_active(False)
        self.transcribe_setpos_check.connect("toggled", _on_setpos_toggled)

        def _on_samestart_toggled(check_btn):
            if check_btn.get_active():
                self.transcribe_setpos_check.set_active(False)
        self.transcribe_samestart_check.connect("toggled", _on_samestart_toggled)

        ping_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ping_row.append(Gtk.Label(label="Ping key"))
        self.ping_key_label = Gtk.Label(label=_key_name(self.ping_code), xalign=0)
        self.ping_key_label.add_css_class("monospace")
        self.ping_key_label.set_hexpand(True)
        ping_row.append(self.ping_key_label)
        self.ping_change_button = Gtk.Button(label="Change")
        self.ping_change_button.connect("clicked", self.on_ping_change_clicked)
        ping_row.append(self.ping_change_button)
        root.append(ping_row)

        transcribe_action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.transcribe_button = Gtk.Button(label="Start Transcribing")
        self.transcribe_button.connect("clicked", self.on_transcribe_clicked)
        transcribe_action_row.append(self.transcribe_button)
        self.transcribe_status_label = Gtk.Label(label="", xalign=0)
        transcribe_action_row.append(self.transcribe_status_label)
        root.append(transcribe_action_row)

        self.connect("destroy", lambda *_: self._transcriber and self._transcriber.stop())

        # Ignore toggles -- checking one wraps this macro's compiled
        # code in a try/finally that calls ignore(target) on entry and
        # ignore(target) again (toggling it back off) on exit. Same
        # machinery as calling ignore() by hand (see the Function
        # reference below), just automatic and guaranteed to restore
        # correctly even if the macro raises partway through.
        ignore_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.ignore_kb_check = Gtk.CheckButton(label="Ignore Keyboard Input (except Abort)")
        self.ignore_kb_check.set_active(bool(self.macro.get("ignore_keyboard", False)))
        ignore_row.append(self.ignore_kb_check)
        self.ignore_mouse_btn_check = Gtk.CheckButton(label="Ignore Mouse Buttons")
        self.ignore_mouse_btn_check.set_active(bool(self.macro.get("ignore_mouse_buttons", False)))
        ignore_row.append(self.ignore_mouse_btn_check)
        self.ignore_mouse_move_check = Gtk.CheckButton(label="Ignore Mouse Movement")
        self.ignore_mouse_move_check.set_active(bool(self.macro.get("ignore_mouse_movement", False)))
        ignore_row.append(self.ignore_mouse_move_check)
        root.append(ignore_row)
        ignore_hint = Gtk.Label(
            xalign=0,
            label="Actually grabs the real device while this macro runs, so your own "
                  "input can't interfere with it -- a bigger deal than the other "
                  "settings here. The abort hotkey always force-releases these if "
                  "something gets stuck.",
        )
        ignore_hint.add_css_class("dim-label")
        ignore_hint.set_wrap(True)
        root.append(ignore_hint)

        # Dictionary
        expander = Gtk.Expander(label="Function reference")
        dict_label = Gtk.Label(label=DICTIONARY_TEXT, xalign=0, wrap=True)
        expander.set_child(dict_label)
        root.append(expander)

        # Simplified-name reference (only really relevant with the
        # checkbox in the code header on the right, but harmless to
        # always show)
        simplified_expander = Gtk.Expander(label="Simplified name reference")
        simplified_label = Gtk.Label(label=_simplified_names_reference_text(), xalign=0, wrap=True)
        simplified_label.add_css_class("monospace")
        simplified_expander.set_child(simplified_label)
        root.append(simplified_expander)

        # Custom button names -- app-wide aliases layered on top of the
        # built-in simplified names, saved alongside this macro on Save.
        root.append(Gtk.Label(label="Custom button names", xalign=0))
        self.aliases_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        root.append(self.aliases_box)

        add_alias_btn = Gtk.Button(label="+ Add custom name")
        add_alias_btn.connect("clicked", lambda *_: self._add_alias_row())
        root.append(add_alias_btn)

        self._alias_rows = []  # [(name_entry, target_dropdown, row_widget), ...]
        self._alias_target_names = sorted(set(md.SIMPLIFIED_NAMES.keys()))

        existing_aliases = md.load_aliases().get("aliases", {})
        if existing_aliases:
            for custom_name, target in existing_aliases.items():
                self._add_alias_row(custom_name, target)
        else:
            self._add_alias_row()

        # Code -- the entire right-hand pane, per the deliberate split
        # above: this is what you're looking at 90% of the time while
        # working on a macro, so it gets the dedicated real estate
        # rather than competing for scroll space with everything else.
        code_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        code_header.append(Gtk.Label(label="Macro code", xalign=0))
        self.simplified_check = Gtk.CheckButton(label="Simplified Variable Names")
        self.simplified_check.set_active(bool(self.macro.get("simplified_names", False)))
        code_header.append(self.simplified_check)
        right_content.append(code_header)

        code_frame = Gtk.Frame()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.code_view = Gtk.TextView()
        self.code_view.set_monospace(True)
        self.code_view.add_css_class("view")
        self.code_view.set_top_margin(6)
        self.code_view.set_bottom_margin(6)
        self.code_view.set_left_margin(6)
        self.code_view.set_right_margin(6)
        self.code_view.get_buffer().set_text(self.macro.get("code", ""))
        scroller.set_child(self.code_view)
        code_frame.set_child(scroller)
        right_content.append(code_frame)

    def _combo_display(self, combo_names):
        if not combo_names:
            return "(none set)"
        return " + ".join(combo_names)

    def _on_mouse_pos_update(self, text):
        self.mouse_pos_label.set_label(text)

    def on_record_clicked(self, _btn):
        if self._recording:
            if self._recorder:
                self._recorder.cancel()
            return

        keyboard_path = self.app_window.keyboard_path
        mouse_path = self.app_window.mouse_path
        if not keyboard_path and not mouse_path:
            self.combo_label.set_text("Set a keyboard/mouse device path first.")
            return

        self._recording = True
        self.record_button.set_label("Recording… (click to cancel)")
        self._recorder = ComboRecorder(
            keyboard_path, mouse_path,
            on_update=self._on_combo_update,
            on_done=self._on_combo_done,
        )
        self._recorder.start()

    def _on_combo_update(self, held_codes, elapsed_stable):
        names = [_key_name(c) for c in held_codes]
        if not names:
            self.combo_label.set_text("(listening… press and hold your combo)")
        else:
            remaining = max(0.0, ComboRecorder.STABLE_SECONDS - elapsed_stable)
            if remaining > 0:
                self.combo_label.set_text(
                    f"{self._combo_display(names)}  — hold steady ({remaining:.1f}s left)"
                )
            else:
                self.combo_label.set_text(f"{self._combo_display(names)}  — captured!")
        return False

    def _on_combo_done(self, combo_names, status):
        self._recording = False
        self.record_button.set_label("Record Combo")
        if status == "ok":
            self.macro["combo"] = combo_names
            self.combo_label.set_text(self._combo_display(combo_names))
        elif status == "no_devices":
            self.combo_label.set_text("Couldn't open keyboard/mouse device -- check paths & permissions.")
        else:  # cancelled
            self.combo_label.set_text(self._combo_display(self.macro.get("combo", [])))
        return False

    def on_ping_change_clicked(self, _btn):
        self.ping_change_button.set_sensitive(False)
        self.ping_key_label.set_text("press a key…")
        detect_ping_key(
            self.app_window.keyboard_path, self.app_window.mouse_path,
            on_found=self._on_ping_found,
        )

    def _on_ping_found(self, code, name):
        self.ping_change_button.set_sensitive(True)
        if code is None:
            self.ping_key_label.set_text(_key_name(self.ping_code))
            self.transcribe_status_label.set_text("No key detected -- kept the previous ping key.")
            return False
        self.ping_code = code
        self.ping_key_label.set_text(name)
        self.app_window.state["transcribe_ping_key"] = name
        try:
            md.save_state(self.app_window.state)
        except Exception as exc:
            self.transcribe_status_label.set_text(f"Ping key set, but couldn't save: {exc}")
        return False

    def on_transcribe_clicked(self, _btn):
        if self._transcribing:
            if self._transcriber:
                self._transcriber.stop()
            return

        transcribe_kb = self.transcribe_kb_check.get_active()
        transcribe_mouse = self.transcribe_mouse_check.get_active()
        if not transcribe_kb and not transcribe_mouse:
            self.transcribe_status_label.set_text("Check Transcribe Keyboard and/or Transcribe Mouse first.")
            return

        keyboard_path = self.app_window.keyboard_path
        mouse_path = self.app_window.mouse_path
        if (transcribe_kb and not keyboard_path) or (transcribe_mouse and not mouse_path):
            self.transcribe_status_label.set_text("Set a keyboard/mouse device path first.")
            return

        self._transcribing = True
        self.transcribe_button.set_label("Stop Transcribing")
        self.transcribe_status_label.set_text("Transcribing… inserting code at your cursor position.")
        self._transcriber = InputTranscriber(
            keyboard_path, mouse_path,
            transcribe_keyboard=transcribe_kb,
            transcribe_mouse=transcribe_mouse,
            raw_mouse=self.transcribe_raw_check.get_active(),
            set_positions=self.transcribe_setpos_check.get_active(),
            same_start=self.transcribe_samestart_check.get_active(),
            raw_hz=self.raw_hz_spin.get_value(),
            ping_code=self.ping_code,
            on_line=self._on_transcribe_line,
            on_done=self._on_transcribe_done,
        )
        self._transcriber.start()

    def _on_transcribe_line(self, text):
        buf = self.code_view.get_buffer()
        buf.insert_at_cursor(text)
        # Rapid successive programmatic inserts (raw mode can fire this
        # up to raw_hz times a second) seem to occasionally outrun
        # GTK's own change-driven repaint -- explicitly forcing a
        # redraw and keeping the cursor's line in view avoids stale/
        # ghosted text lingering in the view during a transcribe run.
        self.code_view.queue_draw()
        self.code_view.scroll_mark_onscreen(buf.get_insert())
        return False

    def _on_transcribe_done(self, status):
        self._transcribing = False
        self.transcribe_button.set_label("Start Transcribing")
        if status == "no_devices":
            self.transcribe_status_label.set_text("Couldn't open keyboard/mouse device -- check paths & permissions.")
        else:
            self.transcribe_status_label.set_text("Stopped.")
        return False

    def _add_alias_row(self, name="", target=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_entry = Gtk.Entry(text=name, hexpand=True)
        row.append(name_entry)
        row.append(Gtk.Label(label="="))
        dropdown = Gtk.DropDown.new_from_strings(self._alias_target_names)
        if target in self._alias_target_names:
            dropdown.set_selected(self._alias_target_names.index(target))
        row.append(dropdown)

        remove_btn = Gtk.Button(label="Remove")

        def do_remove(_b):
            self.aliases_box.remove(row)
            self._alias_rows.remove((name_entry, dropdown, row))

        remove_btn.connect("clicked", do_remove)
        row.append(remove_btn)

        self.aliases_box.append(row)
        self._alias_rows.append((name_entry, dropdown, row))

    def _do_save(self):
        """Does the actual save work; returns True on success, False
        if validation failed (error_label is already set either way).
        Shared by on_save_clicked and on_save_and_close_clicked -- the
        only difference between them is whether this gets followed by
        self.close()."""
        buf = self.code_view.get_buffer()
        code_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

        self.macro["name"] = self.name_entry.get_text().strip() or "Unnamed Macro"
        self.macro["description"] = self.desc_entry.get_text().strip()
        self.macro["repeat_mode"] = REPEAT_MODES[self.repeat_dropdown.get_selected()]
        self.macro["code"] = code_text
        self.macro["simplified_names"] = self.simplified_check.get_active()
        self.macro["ignore_keyboard"] = self.ignore_kb_check.get_active()
        self.macro["ignore_mouse_buttons"] = self.ignore_mouse_btn_check.get_active()
        self.macro["ignore_mouse_movement"] = self.ignore_mouse_move_check.get_active()

        # Validate: this only compiles the body into a function, it never
        # executes it, so it's safe to run without /dev/uinput access.
        try:
            md.compile_macro(self.macro)
        except Exception as exc:
            self.error_label.set_text(f"Code error, not saved: {exc}")
            return False

        # Custom button names are app-wide, saved alongside this macro.
        aliases = {}
        for name_entry, dropdown, _row in self._alias_rows:
            custom_name = name_entry.get_text().strip()
            if not custom_name:
                continue
            idx = dropdown.get_selected()
            if 0 <= idx < len(self._alias_target_names):
                aliases[custom_name] = self._alias_target_names[idx]
        md.save_aliases({"aliases": aliases})

        # Transcription settings are app-wide too (see the comment where
        # they're loaded, above) -- persisted here so they're restored
        # next time any macro editor opens, not just this one.
        self.app_window.state["transcribe_keyboard"] = self.transcribe_kb_check.get_active()
        self.app_window.state["transcribe_mouse"] = self.transcribe_mouse_check.get_active()
        self.app_window.state["transcribe_raw"] = self.transcribe_raw_check.get_active()
        self.app_window.state["transcribe_setpos"] = self.transcribe_setpos_check.get_active()
        self.app_window.state["transcribe_samestart"] = self.transcribe_samestart_check.get_active()
        self.app_window.state["transcribe_raw_hz"] = self.raw_hz_spin.get_value()
        try:
            md.save_state(self.app_window.state)
        except Exception as exc:
            self.error_label.set_text(f"Macro saved, but transcription settings weren't: {exc}")

        self.on_saved(self.macro, self.is_new)
        self.is_new = False  # a second Save (without closing) is now an edit, not a fresh create
        self.set_title(f"Edit Macro — {self.macro.get('name', '')}")
        self.error_label.set_text("")
        return True

    def on_save_clicked(self, _btn):
        self._do_save()  # stays open either way, per the Save/Close split above

    def on_save_and_close_clicked(self, _btn):
        if self._do_save():
            self.close()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MacroRow(Gtk.Box):
    def __init__(self, macro, enabled, keyboard_path, mouse_path,
                 on_edit, on_delete, on_toggle_enabled, on_repeat_changed, on_combo_changed):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_top=4, margin_bottom=4, margin_start=8, margin_end=8)
        self.macro = macro
        self.keyboard_path = keyboard_path
        self.mouse_path = mouse_path
        self.on_combo_changed = on_combo_changed
        self._recorder = None
        self._recording = False

        name_label = Gtk.Label(label=macro.get("name", "(unnamed)"), xalign=0)
        name_label.set_hexpand(True)
        self.append(name_label)

        self.combo_btn = Gtk.Button(label=self._combo_display(macro.get("combo", [])))
        self.combo_btn.set_tooltip_text("Click, then hold your combo for 3 seconds to change it")
        self.combo_btn.connect("clicked", self._on_combo_btn_clicked)
        self.append(self.combo_btn)

        enabled_switch = Gtk.Switch(active=bool(enabled))
        enabled_switch.set_valign(Gtk.Align.CENTER)
        enabled_switch.connect("state-set", lambda sw, state: (on_toggle_enabled(macro, state), False)[1])
        self.append(enabled_switch)

        repeat_dropdown = Gtk.DropDown.new_from_strings(REPEAT_MODE_LABELS)
        mode = macro.get("repeat_mode", "none")
        repeat_dropdown.set_selected(REPEAT_MODES.index(mode) if mode in REPEAT_MODES else 0)
        repeat_dropdown.connect(
            "notify::selected",
            lambda dd, _p: on_repeat_changed(macro, REPEAT_MODES[dd.get_selected()]),
        )
        self.append(repeat_dropdown)

        edit_btn = Gtk.Button(label="Edit")
        edit_btn.connect("clicked", lambda *_: on_edit(macro))
        self.append(edit_btn)

        delete_btn = Gtk.Button(label="Delete")
        delete_btn.connect("clicked", lambda *_: on_delete(macro))
        self.append(delete_btn)

    def _combo_display(self, combo_names):
        return " + ".join(combo_names) if combo_names else "(no combo)"

    def _on_combo_btn_clicked(self, _btn):
        if self._recording:
            if self._recorder:
                self._recorder.cancel()
            return
        if not self.keyboard_path and not self.mouse_path:
            self.combo_btn.set_label("Set device paths first")
            return

        self._recording = True
        self.combo_btn.set_label("Recording… (click to cancel)")
        self._recorder = ComboRecorder(
            self.keyboard_path, self.mouse_path,
            on_update=self._on_combo_update,
            on_done=self._on_combo_done,
        )
        self._recorder.start()

    def _on_combo_update(self, held_codes, elapsed_stable):
        names = [_key_name(c) for c in held_codes]
        if not names:
            self.combo_btn.set_label("(listening…)")
        else:
            remaining = max(0.0, ComboRecorder.STABLE_SECONDS - elapsed_stable)
            if remaining > 0:
                self.combo_btn.set_label(f"{self._combo_display(names)} ({remaining:.1f}s)")
            else:
                self.combo_btn.set_label(f"{self._combo_display(names)} \u2713")
        return False

    def _on_combo_done(self, combo_names, status):
        self._recording = False
        if status == "ok":
            self.macro["combo"] = combo_names
            self.combo_btn.set_label(self._combo_display(combo_names))
            self.on_combo_changed(self.macro, combo_names)
        elif status == "no_devices":
            self.combo_btn.set_label("Device error")
        else:  # cancelled
            self.combo_btn.set_label(self._combo_display(self.macro.get("combo", [])))
        return False


class ProfileRow(Gtk.Box):
    def __init__(self, profile_id, name, is_active, can_move_up, can_move_down,
                 on_select, on_rename, on_delete, on_move_up, on_move_down):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                          margin_top=2, margin_bottom=2, margin_start=8, margin_end=8)
        self.profile_id = profile_id

        up_btn = Gtk.Button(label="\u25B2")  # ▲
        up_btn.set_sensitive(can_move_up)
        up_btn.set_tooltip_text("Move up")
        up_btn.connect("clicked", lambda *_: on_move_up(profile_id))
        self.append(up_btn)

        down_btn = Gtk.Button(label="\u25BC")  # ▼
        down_btn.set_sensitive(can_move_down)
        down_btn.set_tooltip_text("Move down")
        down_btn.connect("clicked", lambda *_: on_move_down(profile_id))
        self.append(down_btn)

        select_label = ("\u2713 " if is_active else "") + name
        select_btn = Gtk.Button(label=select_label)
        select_btn.set_hexpand(True)
        if is_active:
            select_btn.add_css_class("suggested-action")
        select_btn.connect("clicked", lambda *_: on_select(profile_id))
        self.append(select_btn)

        rename_btn = Gtk.Button(label="\u270e")  # pencil
        rename_btn.set_tooltip_text("Rename")
        rename_btn.connect("clicked", lambda *_: on_rename(profile_id))
        self.append(rename_btn)

        delete_btn = Gtk.Button(label="-")
        delete_btn.set_tooltip_text("Delete")
        delete_btn.connect("clicked", lambda *_: on_delete(profile_id))
        self.append(delete_btn)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app, testing=False):
        super().__init__(application=app, title="Puppetry")
        _ensure_zoom_registered()
        # The application_id (see MacroApp below) handles taskbar/
        # launcher/alt-tab icon resolution via the .desktop file, but
        # NOT the titlebar/CSD corner icon -- GTK draws that itself
        # (its own client-side-decoration fallback, used since Hyprland
        # doesn't support server-side decoration) from this call
        # specifically. gtk_window_set_icon_name() is one of the few
        # icon-related GtkWindow methods GTK4 kept (the pixbuf/file
        # variants like set_icon()/set_icon_from_file() were removed;
        # this name-based one wasn't).
        self.set_icon_name("puppetry")
        # set_default_size is still worth keeping as a fallback for
        # window managers that don't honor maximize() for whatever
        # reason -- maximize() itself is a request, not a guarantee.
        self.set_default_size(760, 720 if testing else 640)
        self.maximize()
        self.testing = testing

        md.ensure_config_exists()
        self.state = md.load_state()
        self.profile_id = self.state.get("active_profile") or "profile_1"
        self.macros_data = md.load_macros()          # shared across all profiles
        self.profile = md.load_profile(self.profile_id)  # just {name, enabled: {id: bool}}
        self.dirty = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(root)

        # Status/save row (profile picker itself is a row list below,
        # not a dropdown -- see the Profiles section)
        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_label = Gtk.Label(label="")
        self.status_label.set_hexpand(True)
        profile_row.append(self.status_label)

        save_btn = Gtk.Button(label="Save (restarts daemon)")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self.on_save_clicked)
        profile_row.append(save_btn)
        profile_row.append(_make_zoom_buttons())

        # Persisted in state.json so this survives reopening the app --
        # previously reset to off every launch.
        self.autosave_check = Gtk.CheckButton(label="Auto-save on change?")
        self.autosave_check.set_active(bool(self.state.get("autosave", False)))
        self.autosave_check.connect("toggled", self.on_autosave_toggled)
        profile_row.append(self.autosave_check)
        root.append(profile_row)

        # Profiles -- a row list (not a dropdown) so rename/delete/reorder
        # controls can sit directly next to each profile's name.
        root.append(Gtk.Label(label="Profiles", xalign=0))
        self.profile_list_box = Gtk.ListBox()
        root.append(self.profile_list_box)

        add_profile_btn = Gtk.Button(label="+ New Profile")
        add_profile_btn.connect("clicked", self.on_add_profile)
        root.append(add_profile_btn)

        self.refresh_profile_list()

        # Device paths -- self.keyboard_path/keyboard_name (and mouse
        # equivalents) are the real source of truth; the Label just
        # displays whichever the eye toggle currently wants shown.
        self.keyboard_path = self.state.get("keyboard_path") or None
        self.keyboard_name = self.state.get("keyboard_name") or None
        self.mouse_path = self.state.get("mouse_path") or None
        self.mouse_name = self.state.get("mouse_name") or None

        # Backfill: upgrading from a state.json written before names
        # were tracked -- resolve them now so the display shows names
        # immediately rather than falling back to raw paths.
        if self.keyboard_path and not self.keyboard_name:
            try:
                self.keyboard_name = InputDevice(self.keyboard_path).name
            except Exception:
                pass
        if self.mouse_path and not self.mouse_name:
            try:
                self.mouse_name = InputDevice(self.mouse_path).name
            except Exception:
                pass

        dev_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dev_row.append(Gtk.Label(label="Keyboard"))
        self.keyboard_display = Gtk.Label(xalign=0, hexpand=True)
        dev_row.append(self.keyboard_display)
        self.keyboard_eye_btn = Gtk.ToggleButton(label="\U0001F441")
        self.keyboard_eye_btn.set_tooltip_text("Show raw device path instead of name")
        self.keyboard_eye_btn.connect("toggled", lambda *_: self._update_device_display("keyboard"))
        dev_row.append(self.keyboard_eye_btn)
        kb_detect = Gtk.Button(label="Detect (press a key)")
        kb_detect.connect("clicked", lambda btn: self.on_detect_clicked("keyboard", btn))
        dev_row.append(kb_detect)
        root.append(dev_row)

        dev_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dev_row2.append(Gtk.Label(label="Mouse   "))
        self.mouse_display = Gtk.Label(xalign=0, hexpand=True)
        dev_row2.append(self.mouse_display)
        self.mouse_eye_btn = Gtk.ToggleButton(label="\U0001F441")
        self.mouse_eye_btn.set_tooltip_text("Show raw device path instead of name")
        self.mouse_eye_btn.connect("toggled", lambda *_: self._update_device_display("mouse"))
        dev_row2.append(self.mouse_eye_btn)
        mouse_detect = Gtk.Button(label="Detect (move or click)")
        mouse_detect.connect("clicked", lambda btn: self.on_detect_clicked("mouse", btn))
        dev_row2.append(mouse_detect)
        root.append(dev_row2)

        self._update_device_display("keyboard")
        self._update_device_display("mouse")

        # Panic-button hotkey -- daemon-wide, not per-macro, so it
        # lives here rather than in the macro editor. Takes effect on
        # next daemon restart (Save button below, or a manual
        # `systemctl --user restart macro-daemon`) since it's only
        # read once at daemon startup. Follows the same "only persists
        # on Save" pattern as the keyboard/mouse device rows above --
        # self.abort_key_name is the pending value, only written into
        # self.state (and disk) from on_save_clicked.
        self.abort_key_name = self.state.get("abort_key") or "KEY_PAUSE"
        abort_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        abort_row.append(Gtk.Label(label="Abort key"))
        self.abort_key_label = Gtk.Label(label=self.abort_key_name, xalign=0, hexpand=True)
        self.abort_key_label.add_css_class("monospace")
        abort_row.append(self.abort_key_label)
        abort_change_btn = Gtk.Button(label="Change")
        abort_change_btn.connect("clicked", self.on_abort_key_change_clicked)
        abort_row.append(abort_change_btn)
        root.append(abort_row)
        abort_hint = Gtk.Label(
            xalign=0,
            label="Instantly stops every running macro and releases any stuck keys/"
                  "clicks. Takes effect after the next Save/restart.",
        )
        abort_hint.add_css_class("dim-label")
        root.append(abort_hint)

        root.append(Gtk.Separator())

        # Macro list
        root.append(Gtk.Label(label="Macros", xalign=0))
        self.list_box = Gtk.ListBox()
        list_scroller = Gtk.ScrolledWindow(vexpand=True)
        list_scroller.set_child(self.list_box)
        root.append(list_scroller)

        add_btn = Gtk.Button(label="+ Add Macro")
        add_btn.connect("clicked", self.on_add_macro)
        root.append(add_btn)

        self.refresh_macro_list()

        if self.testing:
            root.append(Gtk.Separator())
            root.append(Gtk.Label(label="Test -- all input devices (--testing)", xalign=0))
            self.test_list_box = Gtk.ListBox()
            test_scroller = Gtk.ScrolledWindow(min_content_height=160)
            test_scroller.set_child(self.test_list_box)
            root.append(test_scroller)
            self.refresh_test_panel()

    # -- macro list management -------------------------------------------------

    def refresh_macro_list(self):
        child = self.list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list_box.remove(child)
            child = nxt

        enabled_map = self.profile.get("enabled", {})
        keyboard_path = self.keyboard_path or ""
        mouse_path = self.mouse_path or ""
        for macro in self.macros_data.get("macros", []):
            row = MacroRow(
                macro,
                enabled_map.get(macro.get("id"), False),
                keyboard_path,
                mouse_path,
                on_edit=self.on_edit_macro,
                on_delete=self.on_delete_macro,
                on_toggle_enabled=self.on_toggle_enabled,
                on_repeat_changed=self.on_repeat_changed,
                on_combo_changed=self.on_combo_changed,
            )
            self.list_box.append(row)

    def on_combo_changed(self, macro, combo_names):
        target = self._find_macro(macro["id"])
        if target is not None:
            target["combo"] = combo_names
            self._mark_dirty()

    def _find_macro(self, macro_id):
        for m in self.macros_data.get("macros", []):
            if m.get("id") == macro_id:
                return m
        return None

    def _mark_dirty(self):
        self.dirty = True
        if self.autosave_check.get_active():
            self.on_save_clicked(None)
        else:
            self.status_label.set_text("Unsaved changes")

    def on_autosave_toggled(self, check_btn):
        # Persisted immediately (not gated behind the Save button) since
        # this is a UI preference, not part of the macro/profile data
        # itself -- previously this reset to off every time the app
        # reopened because nothing saved it anywhere.
        self.state["autosave"] = check_btn.get_active()
        try:
            md.save_state(self.state)
        except Exception:
            pass  # non-critical -- worst case the preference doesn't persist this run

    def on_toggle_enabled(self, macro, state):
        self.profile.setdefault("enabled", {})[macro["id"]] = state
        self._mark_dirty()

    def on_repeat_changed(self, macro, mode):
        target = self._find_macro(macro["id"])
        if target is not None:
            target["repeat_mode"] = mode
            self._mark_dirty()

    def on_edit_macro(self, macro):
        editor = MacroEditorWindow(self, macro, is_new=False, on_saved=self.on_macro_saved)
        editor.present()

    def on_add_macro(self, _btn):
        blank = _blank_macro()
        editor = MacroEditorWindow(self, blank, is_new=True, on_saved=self.on_macro_saved)
        editor.present()

    def on_macro_saved(self, macro, is_new):
        if is_new:
            self.macros_data.setdefault("macros", []).append(macro)
        else:
            target = self._find_macro(macro["id"])
            if target is not None:
                target.update(macro)
        self.refresh_macro_list()
        self._mark_dirty()

    def on_delete_macro(self, macro):
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Delete '{macro.get('name', 'this macro')}'?")
        dialog.set_buttons(["Cancel", "Delete"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def on_response(_dialog, result):
            try:
                choice = dialog.choose_finish(result)
            except Exception:
                return
            if choice == 1:
                self.macros_data["macros"] = [
                    m for m in self.macros_data.get("macros", []) if m.get("id") != macro.get("id")
                ]
                self.profile.get("enabled", {}).pop(macro.get("id"), None)
                self.refresh_macro_list()
                self._mark_dirty()

        dialog.choose(self, None, on_response)

    # -- profile switching -------------------------------------------------

    def _ordered_profiles(self):
        """md.list_profile_ids() sorted by the user's chosen order
        (state["profile_order"]), with any not-yet-tracked profiles
        (freshly created, or from before ordering existed) appended at
        the end in their natural scan order."""
        raw = md.list_profile_ids()
        raw_map = dict(raw)
        order = self.state.get("profile_order") or []
        ordered_ids = [pid for pid in order if pid in raw_map]
        for pid, _name in raw:
            if pid not in ordered_ids:
                ordered_ids.append(pid)
        return [(pid, raw_map[pid]) for pid in ordered_ids]

    def refresh_profile_list(self):
        self.profiles = self._ordered_profiles()

        child = self.profile_list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.profile_list_box.remove(child)
            child = nxt

        n = len(self.profiles)
        for i, (pid, name) in enumerate(self.profiles):
            row = ProfileRow(
                pid, name,
                is_active=(pid == self.profile_id),
                can_move_up=(i > 0),
                can_move_down=(i < n - 1),
                on_select=self.on_profile_row_select,
                on_rename=self.on_profile_row_rename,
                on_delete=self.on_profile_row_delete,
                on_move_up=lambda p: self.on_profile_move(p, -1),
                on_move_down=lambda p: self.on_profile_move(p, 1),
            )
            self.profile_list_box.append(row)

    def on_profile_row_select(self, profile_id):
        if profile_id == self.profile_id:
            return

        if self.dirty:
            alert = Gtk.AlertDialog()
            alert.set_message("You have unsaved changes.")
            alert.set_buttons(["Cancel", "Discard", "Save"])
            alert.set_cancel_button(0)

            def on_response(_d, result):
                try:
                    choice = alert.choose_finish(result)
                except Exception:
                    choice = 0
                if choice == 1:  # Discard
                    self._switch_profile(profile_id)
                    self.refresh_profile_list()
                elif choice == 2:  # Save
                    self.on_save_clicked(None)
                    self._switch_profile(profile_id)
                    self.refresh_profile_list()
                # Cancel: nothing to revert -- the list was never touched.

            alert.choose(self, None, on_response)
        else:
            self._switch_profile(profile_id)
            self.refresh_profile_list()

    def _switch_profile(self, new_id):
        self.profile_id = new_id
        self.profile = md.load_profile(new_id)  # just a different enabled-map; macros_data stays as-is
        self.dirty = False
        self.status_label.set_text("")
        self.refresh_macro_list()

    def on_add_profile(self, _btn):
        def do_create(name):
            name = name.strip() or "New Profile"
            new_id = f"profile_{uuid.uuid4().hex[:8]}"
            md.save_profile(new_id, {"name": name, "enabled": {}})
            order = self.state.get("profile_order") or [pid for pid, _n in self.profiles]
            order.append(new_id)
            self.state["profile_order"] = order
            md.save_state(self.state)
            self._switch_profile(new_id)
            self.refresh_profile_list()

        _prompt_text(self, "New profile", "", do_create)

    def on_profile_row_rename(self, profile_id):
        # Renaming never touches self.profile_id or calls
        # _switch_profile -- there's nothing that could "send you back"
        # to another profile as a side effect.
        prof = self.profile if profile_id == self.profile_id else md.load_profile(profile_id)
        current_name = prof.get("name", profile_id)

        def do_rename(name):
            name = name.strip() or current_name
            prof["name"] = name
            md.save_profile(profile_id, prof)
            self.refresh_profile_list()

        _prompt_text(self, "Rename profile", current_name, do_rename)

    def on_profile_row_delete(self, profile_id):
        if len(self.profiles) <= 1:
            self.status_label.set_text("Can't delete the last profile.")
            return

        prof = self.profile if profile_id == self.profile_id else md.load_profile(profile_id)
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Delete profile '{prof.get('name', profile_id)}'? This can't be undone.")
        dialog.set_buttons(["Cancel", "Delete"])
        dialog.set_cancel_button(0)

        def on_response(_d, result):
            try:
                choice = dialog.choose_finish(result)
            except Exception:
                return
            if choice != 1:
                return

            ids = [pid for pid, _n in self.profiles]
            idx = ids.index(profile_id)
            md.delete_profile(profile_id)

            order = self.state.get("profile_order") or []
            if profile_id in order:
                order.remove(profile_id)
            self.state["profile_order"] = order
            md.save_state(self.state)

            if profile_id == self.profile_id:
                # The profile directly above the deleted one (or the
                # new first, if the deleted one was already first) --
                # not always profile 1.
                remaining_ids = [pid for pid in ids if pid != profile_id]
                target_id = remaining_ids[max(0, idx - 1)]
                self._switch_profile(target_id)

            self.refresh_profile_list()

        dialog.choose(self, None, on_response)

    def on_profile_move(self, profile_id, direction):
        ids = [pid for pid, _n in self.profiles]
        idx = ids.index(profile_id)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(ids):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        self.state["profile_order"] = ids
        try:
            md.save_state(self.state)
        except Exception:
            pass
        self.refresh_profile_list()

    # -- device detect -------------------------------------------------

    def refresh_test_panel(self):
        child = self.test_list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.test_list_box.remove(child)
            child = nxt

        for path, name in md.list_all_devices_with_names():
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                           margin_top=4, margin_bottom=4, margin_start=8, margin_end=8)

            label_text = f"{name}  ({path})"
            tags = []
            if path == self.keyboard_path:
                tags.append("current keyboard")
            if path == self.mouse_path:
                tags.append("current mouse")
            if tags:
                label_text += "  [" + ", ".join(tags) + "]"

            name_label = Gtk.Label(label=label_text, xalign=0)
            name_label.set_hexpand(True)
            row.append(name_label)

            kb_btn = Gtk.Button(label="Set as Keyboard")
            kb_btn.connect("clicked", lambda *_a, p=path, n=name: self.on_test_set_device("keyboard", p, n))
            row.append(kb_btn)

            mouse_btn = Gtk.Button(label="Set as Mouse")
            mouse_btn.connect("clicked", lambda *_a, p=path, n=name: self.on_test_set_device("mouse", p, n))
            row.append(mouse_btn)

            self.test_list_box.append(row)

    def on_test_set_device(self, kind, path, name):
        # Immediate + permanent: writes straight to state.json rather
        # than going through the normal dirty/Save flow, since this is
        # a diagnostic tool -- "make it permanent" means right now.
        if kind == "keyboard":
            self.keyboard_path, self.keyboard_name = path, name
            self.state["keyboard_path"], self.state["keyboard_name"] = path, name
        else:
            self.mouse_path, self.mouse_name = path, name
            self.state["mouse_path"], self.state["mouse_name"] = path, name

        try:
            md.save_state(self.state)
            self.status_label.set_text(f"Set {kind}: {name} (saved)")
        except Exception as exc:
            self.status_label.set_text(f"Couldn't save: {exc}")

        self._update_device_display(kind)
        self.refresh_test_panel()
        self.refresh_macro_list()  # rows' combo recorders need the new device paths

    def _update_device_display(self, kind):
        if kind == "keyboard":
            path, name, eye_btn, label = self.keyboard_path, self.keyboard_name, self.keyboard_eye_btn, self.keyboard_display
        else:
            path, name, eye_btn, label = self.mouse_path, self.mouse_name, self.mouse_eye_btn, self.mouse_display

        if not path:
            label.set_text("(not set)")
            return
        if eye_btn.get_active():
            label.set_text(path)
        else:
            label.set_text(name or path)  # fall back to path if name is unknown

    def on_detect_clicked(self, kind, button):
        button.set_sensitive(False)
        button.set_label("Listening…")
        self.status_label.set_text(f"Move or click your {kind} now…" if kind == "mouse" else "Press a key now…")

        def done(path, name):
            button.set_sensitive(True)
            button.set_label("Detect (press a key)" if kind == "keyboard" else "Detect (move or click)")
            if path:
                if kind == "keyboard":
                    self.keyboard_path, self.keyboard_name = path, name
                else:
                    self.mouse_path, self.mouse_name = path, name
                self._update_device_display(kind)
                self.dirty = True
                self.status_label.set_text(f"Detected {kind}: {name or path}")
            else:
                self.status_label.set_text(
                    f"No {kind} input detected in 10s -- device may lack permission "
                    f"(check `input` group / udev rule), or wrong physical device was used."
                )
            return False

        detect_device(kind, done)

    def on_abort_key_change_clicked(self, btn):
        btn.set_sensitive(False)
        btn.set_label("Listening…")
        self.status_label.set_text("Press the key you want as the abort/panic hotkey…")

        def done(code, name):
            btn.set_sensitive(True)
            btn.set_label("Change")
            if code is not None:
                self.abort_key_name = name
                self.abort_key_label.set_text(name)
                self.dirty = True
                self.status_label.set_text(f"Abort key set to {name} -- takes effect after Save.")
            else:
                self.status_label.set_text("No key detected in 10s -- kept the previous abort key.")
            return False

        detect_ping_key(self.keyboard_path, self.mouse_path, on_found=done)

    # -- save -------------------------------------------------

    def on_save_clicked(self, _btn):
        self.state["active_profile"] = self.profile_id
        self.state["keyboard_path"] = self.keyboard_path
        self.state["keyboard_name"] = self.keyboard_name
        self.state["mouse_path"] = self.mouse_path
        self.state["mouse_name"] = self.mouse_name
        self.state["abort_key"] = self.abort_key_name

        try:
            md.save_macros(self.macros_data)
            md.save_profile(self.profile_id, self.profile)
            md.save_state(self.state)
        except Exception as exc:
            self.status_label.set_text(f"Save failed, config unchanged on disk: {exc}")
            return

        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", "macro-daemon.service"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                self.status_label.set_text("Saved. Daemon restarted.")
            else:
                self.status_label.set_text(f"Saved, but restart failed: {result.stderr.strip()}")
        except Exception as exc:
            self.status_label.set_text(f"Saved, but couldn't restart daemon: {exc}")

        self.dirty = False


class MacroApp(Gtk.Application):
    def __init__(self, testing=False):
        # GLib requires application_id to be a dotted, D-Bus-style
        # reverse-DNS name (must contain at least one '.') --
        # "puppetry" alone FAILS that validation. That failure isn't a
        # crash: it's a silent g_return_if_fail that just skips setting
        # the id and prints a GLib-GIO-CRITICAL warning to stderr (easy
        # to miss unless launched from a terminal). Net effect: the app
        # was running with no application id at all, so KWin/Hyprland
        # had nothing valid to correlate against org.puppetry.Puppetry
        # .desktop's icon -- must match startupWMClass in module.nix.
        super().__init__(application_id="org.puppetry.Puppetry")
        self.testing = testing

    def do_activate(self):
        win = MainWindow(self, testing=self.testing)
        win.present()


if __name__ == "__main__":
    testing = "--testing" in sys.argv
    argv = [a for a in sys.argv if a != "--testing"]  # GApplication rejects unknown flags
    app = MacroApp(testing=testing)
    app.run(argv)
