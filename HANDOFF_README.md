# Puppetry — handoff

## What this is

A Linux input-macro system for NixOS: a daemon that watches a
keyboard and mouse device via evdev, matches held-key combos against
a set of user-defined macros, and executes matched macros by writing
synthetic input through two virtual `uinput` devices (one keyboard,
one mouse). Macros are small snippets written against a fixed set of
primitives (`kd`, `ku`, `tap`, `combo`, `move_mouse`, `wheel`, `wait`,
`speed`, `ignore`, `command`, `type`, `actAs`, plus bare
`KEY_*`/`BTN_*` names). A GUI edits the on-disk config and restarts
the daemon service to apply changes; a small control socket lets a
few things (the `puppetry` CLI, the GUI's own combo recorder) talk to
an already-running daemon without a restart.

**As of THIS session, the daemon is a C++ binary (`native/`, embedded
CPython for compatibility) and the GUI is PySide6 (`gui/app.py`), not
the pure-Python daemon + GTK4 GUI described by the rest of this
paragraph historically.** Packaged as a NixOS flake (`flake.nix`) with
a module (`module.nix`) that builds the daemon via CMake, runs it as a
`systemd --user` service, and applies the same udev rules as before.
See "This session's changes" below for the full rewrite, and
"Verified vs. not" for exactly what has and hasn't been exercised.

**This handoff is a continuation of a previous one** — an earlier
`HANDOFF_README.md` already covered a first round of fixes and
features. That document's content has been folded into this one
(see "Prior session" below) rather than kept as a separate file, so
this is now the single source of truth for project state.

## Prior session (folded in from the previous handoff)

**Bug fixes:**
- A long-standing off-by-one in the shifted-digit-symbol table used
  by `type()`: `!` was typed as `)`, `&` as `^`, etc. Fixed and
  verified against all ten symbols.
- Nested macro-calling-macro was capped at one level deep. Fixed with
  a trampoline registry (every macro name resolves to a wrapper that
  looks up the real compiled function from a shared mutable dict at
  *call* time, not compile time) — nesting depth is now unbounded.
- `command()` was silently doing nothing for at least one real
  command: stdout/stderr were piped to `DEVNULL` (any failure was
  invisible), and the daemon's PATH, as a systemd user service, didn't
  reliably include where NixOS keeps binaries. Both fixed —
  stdout/stderr now inherit the daemon's own (surfacing in
  `journalctl --user -u macro-daemon`), and PATH gets a few common
  NixOS binary locations appended.
- An earlier "mouse macro primitives do nothing" report turned out to
  be a disabled input device on the user's end, not a code bug.

**Daemon primitives / language features added:**
- `combo(*keys, time_=0.1)` — chord press (like `tap()` for multiple
  keys/buttons at once). `time_` must be a keyword.
- `command(cmd, *args)` — fire-and-forget shell command via `/bin/sh`
  (never `$SHELL`). Extra positional args are shell-quoted and
  substituted via `.format()`.
- `arguments(name=default, ...)` — an optional first-line declaration
  giving a macro real, renameable parameters. Every parameter needs a
  default (enforced at save/compile time), since a macro can always
  be triggered with zero arguments.
- Macros callable by name from other macros' code, including passing
  arguments, to unlimited nesting depth.
- Per-macro `trigger_edge` (`"down"` default / `"up"`): `"up"` arms
  on full combo completion but only fires once every key in the combo
  has been released. Exposed as an "On Press"/"On Release" dropdown.
  Side benefit: sidesteps the stuck-key problem (see Known issues)
  for a macro's own combo keys specifically, since none are still
  down when it starts.
- Combo-superset suppression: if two enabled macros' combos are both
  satisfied and one is a strict superset of the other, only the more
  specific one fires.

**GUI features added:**
- A control socket (`~/.config/macro-daemon/control.sock`, JSON-line
  protocol: `{"cmd": "FIRE"/"ABORT"/"PAUSE"/"RESUME", ...}`). Used by:
  - `puppetry --name="Macro Name" [arg1 arg2 ...]` — triggers a macro
    by exact display name without restarting the daemon, passing any
    trailing CLI arguments through as positional macro arguments.
  - `puppetry --abort` — same as the panic-button abort hotkey.
  - The GUI's combo recorder sends `PAUSE`/`RESUME` around a recording
    session so an unrelated macro's combo can't fire just because its
    keys happen to be held while a *different* combo is recorded.
- A macro "lock" toggle (🔒/🔓) next to each macro's combo controls
  (main list + editor). Locking disables Edit, Delete, combo
  recording, and the trigger-edge dropdown until unlocked. (This
  replaced an attempted Ctrl+H "hide a specific line" feature that
  didn't work in practice — see Superseded approaches.)
- A "Record Time" control next to the zoom buttons, adjusting how
  long a combo must sit still before the recorder auto-commits it
  (previously a hardcoded 3 seconds). Persisted to `state.json`.
- Ctrl+Scroll to zoom / Ctrl+0 to reset, scoped to the code editor's
  text view specifically (whole-app zoom is planned — see below).

## Session 2 (argument passing) — folded in, superseded by Session 3 below

**Argument passing, made to actually work end to end:**
- The control-socket protocol was rewritten from hand-parsed text
  lines to JSON specifically so arguments can safely carry arbitrary
  strings (spaces, quotes, unicode) without inventing an escaping
  scheme. `send_control_command()` and `_handle_control_client()`
  both updated accordingly.
- `puppetry --name=test wazzap` now actually forwards `wazzap` as a
  positional argument to a macro's `arguments(...)` declaration — the
  CLI parsing existed before but never actually collected or sent
  trailing tokens. Verified end to end against a real `arguments()`
  macro, both with and without an argument supplied (falls back to
  the declared default correctly).
- `_trigger_macro()`/`_fire_once()`/`_loop_until_stopped()`/
  `_start_loop()` all now thread an `args` tuple through, so
  arguments work identically whether a macro is triggered by combo
  (always zero args — nothing to draw them from) or by `puppetry
  --name=... args...` / a `FIRE` control-socket call.

**`command()` argument-passing and further bug-hardening:**
- `command(cmd, *args)`: extra args are shell-quoted and substituted
  into `cmd` via `.format()` placeholders, e.g. `command("mv {0}
  {1}", src, dst)`. Documented with concrete examples (see the
  in-app dictionary and this doc's "How to pass arguments to
  command()" note below, since the user asked this explicitly).
- The prior session's `$SHELL`→`/bin/sh` fix turned out not to be the
  real cause of `command()` failures on this user's system (they're
  on bash, not fish). The actual remaining causes, both fixed this
  session:
  - stdout/stderr were still being piped to `DEVNULL` in one code
    path — now always inherited from the daemon so real failures
    show up in `journalctl`.
  - A systemd user service's PATH doesn't reliably include where
    NixOS actually keeps binaries (this is exactly why `module.nix`
    already had to special-case `kdotool` onto the daemon's PATH).
    `command()` now appends `/run/current-system/sw/bin`,
    `/etc/profiles/per-user/<user>/bin`, and `~/.nix-profile/bin` to
    its subprocess's PATH.
- **Not yet independently reconfirmed against the user's exact
  originally-reported failure** (`powerprofiles set power-saver`
  doing nothing) — reasoned fix, not yet verified live. If it's still
  silent, `journalctl --user -u macro-daemon` right after triggering
  it is the next diagnostic step, now that stderr isn't discarded.

### How to pass arguments to `command()`

For reference, since this came up directly this session:

```python
command("notify-send {0} {1}", "Title here", "a value with spaces")
# runs: notify-send 'Title here' 'a value with spaces'

arguments(profile="balanced")
command("powerprofiles set {0}", profile)
# puppetry --name="Set Power Profile" performance -> profile="performance"
```

CLI/`FIRE`-supplied macro arguments always arrive as strings — cast
with `int(...)`/`float(...)` inside the macro if a declared default is
numeric and the value needs to be used as one.

## Session 3 — the Qt/C++ rewrite, `actAs`, per-key `ignore`

The postponed "Qt + native-core batch" from Session 2 (below) is what
this session tackled: the GTK4→PySide6 GUI port and the daemon's
partial C++ rewrite, done together as planned. Also implemented:
`actAs()` and per-key `ignore_keys()`, two new primitives whose design
was worked out and settled with the user at the start of this session
(see "actAs spec" below) before any code was written.

**New daemon: `native/`, C++17, CMake-built.** Replaces
`macro_daemon.py` entirely. Same on-disk config (`state.json`,
`macros.json`, `profiles/*.json`, `aliases.json`), same control-socket
JSON-line protocol (`FIRE`/`ABORT`/`PAUSE`/`RESUME`), same primitives
and their documented behavior — this is a port, not a redesign, except
where noted below.

- **Key/button names** are no longer a hand-maintained table: `tools/gen_keycodes.py`
  parses THIS BUILD's own `linux/input-event-codes.h` at build time and
  generates the name↔code table CMake compiles in. Keeps up with kernel
  header changes automatically; see that script's own docstring for why.
- **Two execution paths per macro**, chosen by a new `"python_on"` flag
  in macros.json (default `true`):
  - `python_on: true` — genuine embedded CPython (`native/src/python_embed.cpp`).
    Full compatibility with every macro written under the old pure-Python
    daemon; primitives are exposed as real Python callables via a
    `_puppetry_native` C extension module built into the same process.
  - `python_on: false` — the fast native path (`native/src/native_vm.cpp`).
    **Scope decision, per the fallback clause already agreed on for this
    work:** shipped as PRIMITIVES-ONLY (a flat sequence of primitive
    calls per line, `arguments(...)` supported, cross-macro calls by
    name supported) rather than a general if/while/variables
    interpreter. The general version was not attempted first — this was
    the explicit trade-off, not a discovery mid-build. Revisit if
    per-macro branching turns out to matter in practice.
  - A `python_on` macro can call a `python_off` one and vice versa
    (`python_embed.cpp`'s cross-macro trampoline `dynamic_cast`s the
    target and either stays in Python at full fidelity or crosses into
    the native VM with positional-string-only arguments). Verified live
    both directions — see `native/tests/test_mixed_calls.cpp`.
- **`command()`'s `.format()`-based arg substitution** is reimplemented
  in C++ (`format_command()`) rather than shelling out to Python — same
  shell-quoting safety property, same `{0}`/`{1}` placeholder syntax.

### `actAs` spec (settled with the user before implementation)

`actAs(key_pressing, ignore_original, *acting_keys)` — every press of
`key_pressing` also duplicates as presses of the given acting key(s).

- **Runtime-only.** No persistence to `state.json`; a daemon restart
  clears every active mapping.
- **Feedback loops allowed.** Injected acting-key presses flow back
  through ordinary combo matching, so they can trigger other macros
  exactly like a real press would.
- **Global scope**, not per-profile. The abort hotkey clears every
  active `actAs` mapping in addition to whatever else it already does.
- **Held-key transition.** If `key_pressing` is physically down when
  `actAs()` is called (to set OR clear a mapping), the key finishes its
  current press/release cycle acting as whatever it already was; the
  change only takes effect starting from the NEXT press. Implemented as
  a `pending` table in `Runtime`, committed into `active` on that key's
  release (`dispatch.cpp`'s `apply_act_as()`).
- **Toggle-by-repetition.** Calling `actAs()` again with the exact same
  `(key, ignore, acting_keys)` triple clears it back to normal instead
  of reapplying it.
- **`ignore_original`** controls whether the real key's own press is
  suppressed (composed into the same grab/forward machinery `ignore()`
  already uses — an active `ignore_original=true` mapping forces that
  key's device to be grabbed) or passed through alongside the
  duplicates.
- **Per-key `ignore_keys(*codes)`** (function-only, no GUI checkbox, a
  toggle per code like `ignore()`) was added alongside `actAs` so a
  macro can block specific keys/buttons rather than only the whole
  keyboard/mouse.

All of this is unit- and integration-tested — see "Verified vs. not
verified" below; it isn't just a paraphrase of the design conversation.

### GUI: PySide6 port (`gui/`)

Built on the shared `ui_kit/` per `UI_THEMING_GUIDE.md`, which was
already staged in this repo from a prior session and (per its own
26-check `ui_kit_test_kit.py`) still passes standalone. Puppetry-specific
files added this session:

- `gui/app.py` — `MainWindow`, macro list page, macro editor page,
  sidebar nav (`SegmentButton` + `crossfade_to_index`), lock toggle,
  Record Time spinner (`CustomDoubleSpinBox`), trigger-edge/repeat-mode
  dropdowns (`combo_box_stylesheet`), ignore/simplified-names/`python_on`
  checkboxes, and the code editor's own Ctrl+Scroll zoom (Ctrl+0 resets).
  Also the `puppetry --name=.../--abort` CLI entry point, reimplemented
  as a plain socket+JSON client against the daemon's control socket
  (`puppetry_config.send_control_command()`) — the GUI process no longer
  links against the daemon in any way.
- `gui/puppetry_config.py` — reads/writes the exact same on-disk config
  the C++ daemon uses, with the `state.json` mtime-caching the theming
  guide's pitfall #1 explicitly calls for (kit widgets call the theme
  provider from `paintEvent`).
- `gui/combo_recorder.py` — **a fresh implementation**, not a line-for-line
  port: the original GTK combo recorder's source wasn't available to
  read this session (only `README.md`/`HANDOFF_README.md` were), so
  this was reconstructed from the documented PAUSE/RESUME control-socket
  contract and the Record Time spinner's documented "commits after the
  combo sits still for N seconds" behavior. Uses `python-evdev` directly
  against the daemon's own configured keyboard/mouse paths. **Not
  compared side-by-side against the old GTK recorder's actual feel** —
  flagged explicitly so a future session knows to check this first if
  combo recording feels different than before.
- **Not built this session** (unchanged from what Session 2's handoff
  already deferred, not new scope cut here): the Dictionary/Function
  Reference window, Scratch-style block editing, and the eventual
  whole-app Ctrl+Scroll zoom beyond the code editor's own. All three
  were already lower-priority / GUI-toolkit-adjacent items in the
  existing Planned Work list below.

### Verified vs. not verified (read this before trusting any of the above)

This sandbox has no `/dev/input`, no `/dev/uinput`, no display, and no
`nix` binary — so nothing here has been exercised against real
hardware, a real compositor, or a real Nix build. What COULD be
verified, was:

- **Daemon core, unit + live-integration tested, all passing under
  AddressSanitizer + UndefinedBehaviorSanitizer** (`native/tests/`,
  runnable via `ctest` after `cmake --build .`):
  - `test_core`: combo matching + superset suppression, hold/toggle/
    trigger-edge dispatch, `actAs`'s held-key-transition +
    toggle-by-repetition + abort-clears-everything, `arguments(...)`
    extraction. **This is where a real deadlock bug was caught and
    fixed** (`act_as_fn()` was calling into `apply_grab_state()` while
    still holding the mutex `apply_grab_state()` itself locks) — worth
    knowing in case the pattern gets copied into new code.
  - `test_control_socket`: a real Unix-socket client exercising
    `FIRE`/`ABORT`/`PAUSE`/`RESUME` end to end, including malformed-JSON
    and unknown-macro-name error responses.
  - `test_python_embed`: a real embedded interpreter compiling and
    running actual macro source, including `arguments(...)` with a
    numeric default AND a CLI-style string override, `speed()` timing
    verified against a real elapsed-time measurement, and both
    `MacroCompileError` and Python `SyntaxError` surfacing correctly.
    **Also where a second real bug was caught**: `PythonMacroBody`'s
    destructor called `PyGILState_Ensure()` unconditionally, which
    segfaults if it runs after `Py_Finalize()` — fixed with a
    `Py_IsInitialized()` guard, and `main.cpp`/every test now drops
    compiled macros before shutting the interpreter down.
  - `test_native_vm`: primitives-only execution, parameter defaults vs.
    CLI-string overrides, cross-macro-by-name calls, and rejection of
    anything outside the grammar (control flow, unknown names) with a
    per-line error message.
  - `test_mixed_calls`: a `python_on` macro calling a `python_off` one
    by name and back again.
- **GUI, offscreen-tested** (`gui/test_app.py`, `QT_QPA_PLATFORM=offscreen`,
  same technique `ui_kit_test_kit.py` already uses): window construction,
  macro list population, editor field load/save round-trip, lock-toggle
  enable/disable wiring, new-macro creation, Ctrl+Scroll zoom actually
  changing the font, and a scan for unscoped stylesheets (guide pitfall
  #2) across the whole window. 15/15 pass.
- **NOT verified, at all**: real evdev/uinput behavior (device
  detection, grab/ungrab, actual synthetic input reaching a real app),
  the Nix derivations in `flake.nix`/`module.nix` (no `nix` binary
  available — they're a careful draft, not a built package), the combo
  recorder against real hardware, and — per the theming guide's own
  pitfall #12 — **how any of the GUI actually looks on a real display**.
  Offscreen tests catch construction/logic bugs, not layout or visual
  bugs; a real-desktop pass is still owed before calling the GUI done.

## Session 4 — CPS regression fixed, hot-path optimization, C++ transcriber, GUI restored

**CPS regression (the ~16k ceiling after the C++ port, vs ~50k before).**
Root cause: every `wait()` (and every `tap()` hold) went through the
kernel's sleep call, which costs tens of microseconds no matter how small
the request (timer slack + scheduler wakeup). Paid twice per tap cycle,
that floor WAS the ceiling -- verified with a before/after benchmark of
the real daemon code paths (`native/tests/bench_cps.cpp`; in the build
sandbox: ~6.5k tap cycles/s before, ~1.4-3.4M after, depending on VM
load; writes go to /dev/null so the uinput write itself isn't included).
Fixes, all in `native/src`:
- `wait()`: tiny waits spin on the vDSO clock instead of sleeping;
  longer waits sleep to an ABSOLUTE deadline minus a margin (100us, or
  1ms for `precise=True`) then spin the rest. Median error ~0.2us in
  `test_timing`. Waits also share a per-thread timeline, so per-line
  overhead doesn't accumulate as drift (200 x wait(1ms) with 100us of
  work each = 200.1ms). Daemon timer slack set to 1ns (`prctl` in main +
  `TimerSlackNSec=1` in the service).
- uinput: one `write()` per event INCLUDING its SYN_REPORT (was one per
  event plus one per SYN: 4 syscalls per tap -> 2). Mouse moves write X
  and Y in one frame (real diagonals, not stair-steps).
- Key routing/button tables are array lookups (was a heap-allocated
  std::string name + hash lookup per kd()/ku()); synthetic held-key
  tracking is a lock-free atomic bitset (was mutex + std::set).
- Python bindings: METH_FASTCALL (no per-call arg tuple / format parser),
  the GIL is only released around waits >= 200us, and each macro thread
  keeps ONE PyThreadState for its life (was created/destroyed per loop
  iteration).
- Native VM: every line compiled once into a ready-to-call op with
  arguments pre-resolved (was re-resolved into fresh vectors + a hash map
  per line per iteration, and dispatched by string compare). Unknown
  keyword arguments / missing arguments are now compile (save-time)
  errors.
- Grabbed-device forwarding (ignore()/actAs suppression) forwards whole
  hardware frames in one write and reads events in bulk.
**Past the daemon, the compositor and the receiving app are the limit**,
and at very high rates the kernel's per-client evdev buffer overflows and
drops events before any app sees them. That's why the GUI now has an
Input Visualizer / CPS tester (measures what an app actually receives).

**Bugs found and fixed along the way** (each has a regression test):
- Aborting during a Python macro's `tap()`/`wait()`/`combo()` threw a C++
  exception through CPython's C frames (undefined behavior -> crash).
- Aborting during `type(..., async_=True)` / async `move_mouse` let the
  exception escape a background thread -> `std::terminate()` killed the
  daemon.
- `command()` leaked a zombie process per call (the promised reaper
  thread never existed) -- now double-forks; env is built before fork().
- A brand-new macro (id still `null`) failed validation, and a `null`
  `abort_key` in an older state.json would have crashed the daemon at
  startup: nlohmann's `value()` throws on a type mismatch. All config
  reads now go through tolerant `json_str`/`json_bool` helpers.

**Transcription is C++ now** (`native/src/transcriber.*`,
`puppetry-transcribe` binary, launched by the editor; `gui/transcription.py`
streams its output in 50ms batches). Timing comes from the kernel's own
per-event timestamps (microsecond resolution, CLOCK_MONOTONIC via
EVIOCSCLOCKID) -- the finest timing Linux records for input -- and is
written with 6 decimals. The old transcriber timed events when Python
got around to reading them, rounded to 1ms, and DELETED every gap under
20ms (fast sequences replayed compressed); it could also emit a click
before the motion that preceded it. All fixed. New: wheel events are
transcribed; kernel buffer overflows (SYN_DROPPED) are written into the
script as a comment instead of silently corrupting it. The "Precise
timing" toggle records every hardware mouse frame at the mouse's real
polling rate (instead of resampling to the Hz setting) and writes
`precise=True` waits -- most faithful replay, including pointer
acceleration, at the cost of ~2 lines per mouse frame and a busy core on
playback. `test_transcriber` drives the core with synthetic kernel events.

**GUI** (the Session 3 port had silently dropped a lot of the old GTK
app, because macro_gui.py wasn't read then -- all restored):
- Sidebar: Macros / Input Visualizer / Settings.
- Macro rows: `[name box] ... [combo] [x] [On Press/Release] [Repeat]
  [enabled switch] [Edit] [Delete] [lock]` -- click the name box to
  rename inline, click the combo to rebind (click again to cancel), x
  clears it. The lock button wasn't in the requested layout but was kept
  at the far right, since it's the only way to unlock a locked row --
  ask before removing it.
- Settings: profiles (switch / new / rename / delete / reorder -- the
  user chose all of it here rather than a dropdown), devices (Detect,
  name/path toggle, full device list with Set as Keyboard/Mouse), abort
  key, record time, autosave, Save, and the appearance editor.
  Switching profiles now applies immediately (saves + restarts the
  daemon); the old app only did so on the next Save.
- Editor: Save (stays open) / Save and Close / Close bottom-right; Close
  asks about unsaved changes. Save validates with the daemon's own
  compilers (`puppetry-daemon --check`) then applies (writes config and
  restarts the daemon -- which also saves any other pending main-page
  edits). Restored: description, mouse-position readout, transcription,
  ignore toggles, function + simplified-name references (tables come from
  `puppetry-daemon --dump-names`, so they can't drift), custom button
  names.
- Kit tweak (allowed per the guide): `CustomButton` now has a disabled
  look (fill darker(150), dimmed text) -- it had none, so locked rows
  looked clickable.

## Known issues / not yet resolved

- `ignore()` does not reliably release a keyboard key that was already
  held down at the moment its grab kicked in. Deprioritized by
  request. `trigger_edge = "up"` sidesteps this for a macro's own
  combo keys specifically, not the general case.
- The old GTK editor's text "ghosting" during transcription: likely
  gone (the Qt editor inserts transcribed text in 50ms batches instead of
  per line), but not verified on a real display.
- Nothing in Sessions 3-4 has run against real /dev/input, /dev/uinput,
  a compositor, or `nix build` (none exist in the build sandbox). The GUI
  was checked with offscreen tests AND by rendering each page to an image
  and looking at it, but a real-desktop pass is still owed.
- The `command()` PATH/stderr fix from Session 2 still hasn't been
  reconfirmed against the originally-reported `powerprofiles` failure.
- The daemon only reads the active profile at startup, so every Save /
  profile switch restarts it (by design, same as before) -- any running
  hold/toggle loop stops when that happens.

## Superseded approaches (don't retry these)

- Hiding a specific line in the code editor (Ctrl+H) via a GTK
  `invisible` text tag plus a child-anchor placeholder widget.
  Implemented, reported as not working, removed. Root cause never
  diagnosed (no interactive GTK4 available in the environment this
  was built in). Macro-locking (above) was built as the practical
  substitute for the underlying need (keeping specific code out of
  casual view/editing).

## Planned work

The Qt/C++ batch this list used to describe as postponed is now done
(Session 3, above) — see "Verified vs. not verified" for exactly how
far "done" goes. What's left is ordinary next steps, roughly priority
order, now targeting the C++ daemon (`native/`) and the Qt GUI
(`gui/app.py`) directly rather than GTK.

### Next

- `puppetry --name="macro name" --arguments=(arg1, arg2, arg3, ...)`
  — an explicit named-flag syntax for CLI arguments, as an
  alternative/addition to the bare-trailing-args form already
  shipped (`puppetry --name="macro name" arg1 arg2`). Needs a
  decision on whether one replaces the other or both are supported.
- `Wait for Macro Combo` — a macro-code function that pauses
  mid-execution and waits for the macro's own trigger combo to be
  pressed again before continuing.
- `Play Sound` — a macro-code function taking an absolute file path
  (e.g. `/home/mrtw/Desktop/customization/sounds/1.mp3`) and playing
  it.
- `Listen for Button Press` (working name `listenForNextInput()`) — a
  macro-code function that stalls execution until the next keyboard
  or mouse-button input (mouse movement excluded) and returns what
  was pressed, for use in macro-local variables (e.g. `buttonChosen =
  listenForNextInput()`).
- `puppetry --name="macro name" --arguments=(arg1, arg2, arg3, ...)`
  — an explicit named-flag syntax for CLI arguments, as an
  alternative/addition to the bare-trailing-args form already
  shipped (`puppetry --name="macro name" arg1 arg2`). Needs a
  decision on whether one replaces the other or both are supported.
- `Wait for Macro Combo` — a macro-code function that pauses
  mid-execution and waits for the macro's own trigger combo to be
  pressed again before continuing.
- `Play Sound` — a macro-code function taking an absolute file path
  (e.g. `/home/mrtw/Desktop/customization/sounds/1.mp3`) and playing
  it.
- `Listen for Button Press` (working name `listenForNextInput()`) — a
  macro-code function that stalls execution until the next keyboard
  or mouse-button input (mouse movement excluded) and returns what
  was pressed, for use in macro-local variables (e.g. `buttonChosen =
  listenForNextInput()`).
- Replace "Function Reference" with a "Dictionary" button opening a
  separate window: buttons per function, each opening its
  definition, full signature with default arguments, and a call
  example. "Simplified Name Reference" moves into that window's
  bottom-right, changing from a text listing to a keyboard + mouse
  graphic with names overlaid on each button; clicking a button opens
  a popup with its default simplified name (editable) and an
  expandable list of additional alias names. The default simplified
  name is what's used during transcribing when "Simplified Variable
  Names" is on. Now that the Qt port exists, this is ordinary GUI
  work against `gui/app.py`, no longer gated on a toolkit decision.
- Full Scratch-style block-based coding (blocks named after each
  function, dynamically expanding to fit, full copy/paste) as an
  alternative authoring mode alongside raw code editing.
- Whole-app Ctrl+Scroll zoom (window, list, buttons — not just the
  code editor, which already has its own scoped zoom via
  `ZoomablePlainTextEdit` in `gui/editor_page.py`) and Ctrl+0 to reset it.
- A real desktop verification pass on the Qt GUI (see "Verified vs.
  not verified" above) — this is the single highest-priority item,
  since nothing about how it actually looks has been checked yet.
- `module.nix`/`flake.nix` haven't been evaluated against
  a real `nix build` (no `nix` binary in the sandbox this was written
  in) — get a real build working before trusting the derivations.
- On real hardware: compare the Input Visualizer's CPS against the
  daemon-side `bench_cps` number to see where the compositor/app limit
  sits (and whether SYN_DROPPED overflows appear at extreme rates).
- DONE (Session 4, kept so a skim doesn't wonder): CPS ceiling root
  cause, transcription precision, `actAs`, per-key `ignore`.

## Architecture / file map

- `native/` — the daemon (C++17, CMake). See `native/src/*.hpp` for
  the module breakdown (`runtime.hpp` is the shared mutable state,
  `primitives.hpp`/`.cpp` the macro-callable primitives including
  `actAs`, `dispatch.hpp`/`.cpp` the combo-matching + evdev watch loop,
  `macro.hpp`/`.cpp` the repeat-mode/trigger-edge runtime,
  `python_embed.hpp`/`.cpp` the CPython-backed macro path,
  `native_vm.hpp`/`.cpp` the primitives-only fast path,
  `pointer_accel.hpp`/`.cpp` the KDE flat-acceleration setup for our own
  virtual mouse (Session 8),
  `control_socket.hpp`/`.cpp` the daemon's control socket,
  `config.hpp`/`.cpp` the on-disk JSON I/O, `transcriber.hpp`/`.cpp` +
  `transcribe_main.cpp` the `puppetry-transcribe` helper, `main.cpp` the
  daemon entry point incl. `--check` / `--dump-names` / `--list`). `native/tools/gen_keycodes.py` generates the key/button name
  table at build time. `native/tests/` holds the test suite described
  under "Verified vs. not verified" above — `cmake --build . && ctest
  --output-on-failure` from a `native/build/` directory runs all of it.
- `gui/` — the Qt (PySide6) editor:
  - `gui/app.py` — `MainWindow`, sidebar, CLI entry point.
  - `gui/model.py` — `AppModel`: the one in-memory copy of state/macros/
    active profile, dirty tracking, autosave, save, profiles.
  - `gui/macro_list_page.py`, `editor_page.py`, `settings_page.py`,
    `visualizer_page.py` — the pages.
  - `gui/widgets.py` — ToggleSwitch, NameBox, LockToggle, themed
    dialogs, Collapsible, PageBase.
  - `gui/input_tools.py` — combo recorder, device/key detection, mouse
    position readout (ports of the old GTK helpers; the Session 3
    `combo_recorder.py` reconstruction is gone).
  - `gui/transcription.py` — runs `puppetry-transcribe`, and reports
    Puppetry's own window focus to it (Session 8).
  - `gui/reference.py` — function/simplified-name reference text.
  - `gui/puppetry_config.py` — on-disk config, control-socket client,
    helper-binary discovery ($PUPPETRY_BIN_DIR, then native/build, then
    $PATH), macro validation via `puppetry-daemon --check`.
  - `gui/test_app.py` — 85 offscreen checks (`QT_QPA_PLATFORM=offscreen
    python3 gui/test_app.py`); uses the native binaries when built.
  - `gui/sound.py` — plays the transcription start/finish cues via an
    external player (Session 9).
  - `gui/ui_kit/` — the shared PySide6 theming kit (copy, not a shared
    package — see the guide for why). `gui/UI_THEMING_GUIDE.md` is its
    full spec; `gui/ui_kit_test_kit.py` is its own 26-check offscreen
    suite, still passing (only `custom_button.py` was tweaked -- see
    Session 4).
- `module.nix` — NixOS module: builds the C++ daemon via CMake, wraps
  the Qt GUI with a `pyside6`+`evdev` Python, sets up `uinput`
  permissions and the same udev `ID_INPUT_*` classification fix as
  before. **Not evaluated against a real Nix build this session.**
- `flake.nix` — packages the above as a flake output. Same caveat.
- `assets/` — logo images, referenced by the GUI.
- `macro_daemon.py`, `macro_gui.py` — the PREVIOUS (pure-Python
  daemon + GTK4 GUI) implementation. Left in the repo for reference /
  rollback, but no longer what `module.nix`/`flake.nix` build or run.
  Safe to delete once the C++/Qt replacement has had a real-hardware
  verification pass; kept for now specifically so that pass has
  something to compare behavior against if anything regresses.

## Session 5 — abort key stops transcription, transcribe hotkey

Two small additions to transcription, both verified: `native/build`
rebuilt clean (`test_transcriber` 16/16, everything else unaffected), and
`gui/test_app.py` 56/56 (5 new checks) under `QT_QPA_PLATFORM=offscreen`.

**Abort key now ends a running transcription session too.** Previously
the abort key only lived in the daemon (`handle_key_event` in
`dispatch.cpp`) -- `puppetry-transcribe` is a separate process the GUI
spawns and never talked to the daemon or knew about the abort key at all,
so pressing it while transcribing did nothing to the transcript. Fixed at
the transcriber, not the daemon, since it already reads the same devices
independently:
- `TranscriberCore` (`native/src/transcriber.{hpp,cpp}`) gained
  `TranscribeOptions.abort_code` (-1 = disabled) and `abort_requested()`.
  In `feed()`, a press of that code is consumed like the ping key (never
  transcribed) and sets a flag instead of the ping key's cursor-sample
  behavior.
- `transcribe_main.cpp` takes a new `--abort-key KEY_NAME` argument,
  checks `core.abort_requested()` after every batch of fed events, and
  exits **3** (not 0) if that's why it stopped -- a new exit code
  alongside the existing 2 (couldn't open a device) and 64 (bad args).
- `gui/transcription.py`'s `TranscriptionController.start()` takes an
  `abort_key=` kwarg (passed through as `--abort-key`); `_finished()`
  treats exit code 3 as "Stopped: abort key pressed." rather than a
  generic/error stop.
- `gui/editor_page.py` passes `abort_key=self.model.state.get("abort_key")
  or "KEY_PAUSE"` on every `_transcribe_clicked()` start -- always the
  live in-memory abort key, even if Settings hasn't been Saved yet (this
  is just filtering, not daemon config, so there's no restart to wait
  for).

**New "Transcribe hotkey"**, in the editor's Transcribe Inputs section
next to the ping key: press it while the editor page is open to start
transcribing, press it again to stop -- and it's never itself recorded,
including the press that stops a running session.
- Set via a "Change" button that reuses `input_tools.DetectKey` exactly
  like the abort key / ping key pickers elsewhere; stored with
  `model.set_pref("transcribe_hotkey", name)` (GUI-only preference, no
  daemon restart, like the ping key).
- Listening for it while the page is open is a **new** always-on watcher,
  `input_tools.HotkeyListener` -- same shape as `DetectKey` (opens the
  configured keyboard/mouse paths with python-evdev) but doesn't stop
  after one match or time out; it just emits `pressed` every time the
  configured code goes down, for as long as the thread runs. Editor page
  starts/restarts it in `showEvent`/whenever the hotkey or devices change
  (`_restart_hotkey_listener`), and stops it in `hideEvent` alongside the
  mouse-position poller -- so it only does anything while that page is
  actually visible, per the request.
- The hotkey press itself is filtered out of the transcript the same way
  the abort key is: `TranscribeOptions.hotkey_code` (-1 = disabled) is
  consumed silently in `feed()` (no abort-style flag -- the GUI, not the
  subprocess, decides to start/stop). `transcription.py` passes it as
  `--hotkey-key` whenever one is configured.
- `input_tools.resolve_key_code(name)` was added as the reverse of the
  existing `key_name(code)` (uses `evdev.ecodes.ecodes[name]`) so the
  stored hotkey name can be turned back into a code to compare against
  live events.

**Not done / didn't seem asked for:** no "clear hotkey" button (mirrors
the ping key picker, which doesn't have one either); the hotkey is
keyboard/mouse-button only, same universe as the abort key and ping key.
Worth asking the user before adding a way to disable a saved hotkey
without setting a different one, if that comes up next.

## Session 6 — clear-before-transcribing, editor persistence fix, New Macro moved, Ignore Puppetry/Alt+Tab

Verified: `native/build` rebuilt clean (`test_transcriber` now 25/25, +9 new
cases; everything else unaffected), `gui/test_app.py` 63/63 (+7 new
checks) under `QT_QPA_PLATFORM=offscreen`.

**"Clear macro before transcribing"** -- a checkbox in Transcribe Inputs
(`gui/editor_page.py`, `tr_clear`). Purely GUI-side: `_transcribe_clicked()`
wipes `self.code` right before calling `transcriber.start()`, on the
"start" branch only (stopping is unaffected). Persists via
`model.set_pref("transcribe_clear_before", ...)`, same as the other
transcription checkboxes.

**Editor persistence fix (the actual bug behind "keeps the same macro in
it even when you leave").** The editor page was already never destroyed
(it's one QStackedWidget page like the others), and leaving it via the
sidebar already preserved its in-progress fields. The real gap:
`MainWindow.open_editor()` (`gui/app.py`) unconditionally called
`load_macro(macro_id)` -- so clicking "Edit" on the SAME macro again,
after leaving via Settings/Input Visualizer instead of Save/Close, wiped
whatever hadn't been saved yet. Fixed with one guard: if the editor
already holds that exact macro id *and* has unsaved changes, just show
the page as-is; otherwise (different macro, or nothing unsaved) it
reloads fresh as before -- so an edit made elsewhere while the editor sat
idle (e.g. renaming the macro from the list) still shows up correctly.
One knock-on fix: `MacroEditorPage.__init__` now sets `self._snapshot =
self._fingerprint()` at the end of construction (matching the actual
empty-widget state) -- without it, `has_unsaved_changes()` was `True`
from app startup, since `_snapshot` started as `""` and nothing had
called `load_macro()` yet, which would have made the very first
`open_editor()` call go through the "already has unsaved changes" path
for no reason (harmless there since there was nothing to preserve, but
would have broken the new guard above). No behavior change for
Save/Save-and-Close/Close/discard flows -- discard still doesn't revert
the widget fields, same pre-existing quirk as before this session, just
now more visible (a discarded edit resurfaces if you reopen the same
macro, until you actually load a different one). Didn't seem asked for;
worth raising if it's ever confusing in practice.

**"+ New Macro" moved to the bottom-right of the Macros page** (was a
button above the row list). `widgets.PageBase` now exposes
`self.outer_layout` (the page's own top-level layout, outside the
scrolling content) so a subclass can add a fixed bar below the list --
`gui/macro_list_page.py` uses it for a right-aligned bottom bar, same
place the editor puts its Save/Save-and-Close/Close buttons.

**"Ignore Puppetry" and "Ignore Alt+Tab"** -- two more Transcribe Inputs
checkboxes, both implemented in the C++ transcriber (`native/src/
transcriber.{hpp,cpp}`, `transcribe_main.cpp`) since that's what's
actually reading events, not the GUI:
- `TranscribeOptions.ignore_puppetry`: `transcribe_main.cpp` polls the
  active window every 300ms on a detached thread (kdotool, same
  subprocess-pipe pattern the set_positions resync helper already uses --
  never blocks event reading) and calls the new
  `TranscriberCore::set_puppetry_focused(bool, now_us)`. While focused,
  `feed()` drops every event outright; once focus moves away, the paused
  span is added back into `last_us_`/`next_tick_us_`/`next_resync_us_` so
  the next real gap doesn't count the time spent alt-tabbed into Puppetry
  itself. The window check itself is a **new** `primitives.{hpp,cpp}`
  function, `active_window_is_puppetry()` (kdotool `getactivewindow` then
  `getwindowname`, checks for "puppetry" case-insensitively in the
  title -- Qt's own window title, set once in `app.py`). Refactored
  `get_cursor_pos_kde()`'s subprocess-launching code into a shared
  `run_kdotool()` helper both functions now call, rather than duplicating
  the fork/pipe/poll dance a third time; same fail-open behavior (kdotool
  missing/erroring just means "never suppress", not a crash or a hang).
- `TranscribeOptions.ignore_alt_tab`: an Alt press (`KEY_LEFTALT`/
  `KEY_RIGHTALT`) is buffered in `feed()` -- NOT transcribed immediately --
  until Alt comes back up. If any Tab event arrives first, the whole
  thing was Alt+Tab: neither key is ever transcribed, including further
  Tab presses if the switcher is held open and cycled. If Alt comes back
  up with no Tab in between, it was just a normal Alt press, transcribed
  at that point (at its ORIGINAL timestamp, so the recorded macro still
  times it correctly) instead of immediately. This means a lone Alt press
  is now emitted slightly late in the output stream when this option is
  on -- acceptable for what it's for, but worth knowing if a transcript
  ever looks reordered around an Alt key.
- Both are CLI flags on `puppetry-transcribe` (`--ignore-puppetry`,
  `--ignore-alt-tab`), plumbed through `gui/transcription.py`'s
  `start()` and set from two more `editor_page.py` checkboxes
  (`tr_ignore_puppetry`, `tr_ignore_alttab`), persisted the same way as
  the rest of the section.

**Not verified for real:** `active_window_is_puppetry()`'s kdotool
command shapes (`getactivewindow`, `getwindowname <id>`) are written by
analogy with xdotool's well-known interface, which kdotool aims to be
compatible with -- but this session's sandbox has no KDE session or
kdotool binary to actually run them against. If focus detection doesn't
work on a real machine, start by checking those two subcommands (`kdotool
getactivewindow`, then `kdotool getwindowname <that id>`) directly in a
terminal.

## Session 7 — Alt+Tab release-order bug, faster Ignore-Puppetry polling, Macro Editor sidebar entry

Verified: `native/build` rebuilt clean (`test_transcriber` now 28/28, +3 new
cases; `test_core` 33, `test_control_socket` 7, `test_python_embed` 8,
`test_native_vm` 5, `test_mixed_calls` 2 all still passing), `gui/test_app.py`
67/67 (+4 new checks) under `QT_QPA_PLATFORM=offscreen`.

**Fixed: "Ignore Alt+Tab" only ignored Alt, Tab still leaked through.** Root
cause was release-order dependence in `TranscriberCore::feed()`
(`native/src/transcriber.cpp`): the original code cleared `alt_pending_` the
instant Alt itself was released, on the assumption Tab would always be
released first (or already be up). People don't reliably release Alt and Tab
in a fixed order -- when Alt came up *first*, Tab's own later release arrived
with `alt_pending_` already false and fell through to normal handling,
transcribing as a lone `ku(KEY_TAB)`. Fixed in two steps (both are now
regression tests in `test_transcriber.cpp`):
1. Added `tab_held_`, tracking whether Tab is still physically down
   independent of `alt_pending_`; the Tab-suppression condition widened to
   `(alt_pending_ || tab_held_)` so Tab's release is still caught even after
   Alt's own pending state is gone.
2. That first fix broke the pre-existing multi-tap-cycle test: it also reset
   `alt_tab_seen_ = false` on every Tab release, so if Alt was held through
   several Tab taps and then released with no Tab currently down, the last
   Tab-up had already (wrongly) cleared `alt_tab_seen_`, and Alt got flushed
   as a normal keypress. Fixed by making `alt_tab_seen_` **sticky** -- only
   the Alt-up resolution branch ever clears it now; Tab's own press/release
   toggles `tab_held_` but never `alt_tab_seen_`.

**Ignore Puppetry now polls focus every 50ms** (was 300ms) --
`kFocusCheckIntervalUs` in `transcribe_main.cpp`. Requested explicitly
("probably every 50ms or 25ms"); picked the safer end of that range given
each check still round-trips through a `kdotool` subprocess spawn on its own
detached thread.

**New: "Macro Editor" sidebar entry**, directly under "Macros"
(`gui/app.py`'s nav loop, `PAGE_EDITOR` now included alongside
`PAGE_MACROS`/`PAGE_VISUALIZER`/`PAGE_SETTINGS`). Clicking it:
- If the editor already has a macro loaded (however it got there -- Edit
  button, +New Macro, or a previous visit to this same entry), just shows
  the editor page as-is, same persistence as everywhere else in the app.
- If nothing's loaded (`editor_page.macro["id"] is None`, i.e. still
  `blank_macro()`), it redirects to the Macros page instead of opening an
  empty editor, and shows a bottom-of-page toast reading exactly "Please
  select a macro." -- new `MacroListPage.show_toast(message, ms=2500)`,
  a `QLabel` + single-shot `QTimer` placed in the page's existing bottom-right
  fixed bar (left of "+ New Macro").
- New `MainWindow._open_editor_from_nav()` implements this; `_nav_clicked()`
  special-cases `idx == PAGE_EDITOR` to call it directly, skipping the
  existing "leaving the editor" unsaved-changes check (irrelevant when
  navigating *to* the editor). `open_editor()` now also explicitly checks
  the `PAGE_EDITOR` nav button on both its "same macro, already showing"
  shortcut and its normal load-and-crossfade path, so the sidebar reflects
  reality regardless of which entry point opened the editor. Fixed a small
  pre-existing quirk in `_nav_clicked()`'s cancel-on-unsaved-changes path
  while touching this: it used to force-check `PAGE_MACROS` even though the
  editor was still the page actually shown; now re-checks `PAGE_EDITOR`.

## Session 8 — pointer acceleration off by default, optimization pass

Verified: full native suite green (`test_core` 33 -> **55**, +22 new;
`test_control_socket` 7, `test_python_embed` 8, `test_native_vm` 5,
`test_mixed_calls` 2, `test_timing`, `test_transcriber` 28 — all still
passing), `gui/test_app.py` **74/74** (+7 new) under
`QT_QPA_PLATFORM=offscreen`.

### The move_to inaccuracy: diagnosed by the user, not a code bug

Reported as "move_mouse with move_to activated seems to be inaccurate now"
on pre-existing macros. **The user found it before this session did**: a
newly-created virtual mouse had picked up KDE's default pointer
acceleration, and it was exact again once that was turned off. No code
defect — so nothing was "fixed" here, but it did motivate the first item
below. (Worth remembering as a diagnosis pattern: the daemon destroys and
recreates its uinput devices on every restart, so a device that KDE has
never seen before starts from KDE's defaults.)

### Pointer acceleration off by default (new, `native/src/pointer_accel.*`)

libinput applies its acceleration curve to any relative pointing device,
our virtual mouse included, and there is **no uinput-side way to opt
out** — the accel profile is a per-device compositor setting. On KDE it
lives in `kcminputrc`, keyed by vendor id, product id and device name, all
three of which we pick ourselves. So the daemon now writes that one group
before creating the mouse (KDE applies a device's saved settings when the
device appears, so writing first means it comes up already unaccelerated):

```
[Libinput][4660][22136][macro-daemon-virtual-mouse]
PointerAcceleration=0
PointerAccelerationProfile=1     # 1 = flat, i.e. 1:1, no curve
```

- **Scope is deliberately narrow.** Exactly that group is touched; every
  other byte of the file (which also holds the user's *real* mouse
  settings) is preserved. Written atomically via temp file + rename, and
  only when the contents would actually change, so a restart doesn't
  rewrite it. `kcminputrc_with_flat_accel()` is a pure text transform
  precisely so it could be unit-tested — 14 of the new `test_core` checks
  cover it, including idempotency, foreign groups surviving, a group that
  follows ours not absorbing our keys, and the trap that
  `PointerAcceleration` is a *prefix* of `PointerAccelerationProfile`.
- **Nothing happens on non-KDE systems**: skipped unless an existing
  `kcminputrc`/`kwinrc` or a KDE-ish `XDG_CURRENT_DESKTOP`/
  `KDE_FULL_SESSION` says KDE is there, rather than littering a config
  file on a GNOME box.
- Opt out with `"disable_pointer_accel": false` in state.json, or the new
  Settings -> Behavior checkbox ("No pointer acceleration on Puppetry's
  virtual mouse", on by default). Applies on the next daemon start; any
  Save restarts it.
- Device names and ids now live in one place (`uinput_device.hpp`'s
  `kVirtual*Name`/`kVirtualVendorId`/`kVirtualProductId`), used by
  `main.cpp`, `evdev_device.cpp`'s auto-detect exclusion list, and the
  kcminputrc group. **Changing any of them moves the KDE settings group a
  user's saved preferences live under** — don't, casually.
- **NOT verified on a real KDE session** (none in this sandbox). The group
  format and `PointerAccelerationProfile=1` are written from knowledge of
  KWin's libinput config, not from observation. If it doesn't take effect,
  check an existing entry for a real mouse in `~/.config/kcminputrc` and
  compare the shape — that file is the ground truth.

### Optimization pass

Measured with `bench_cps` (now also benchmarks transcription):

| | before | after |
|---|---|---|
| tap cycle, python_off | 2.47M/s | **2.52M/s** |
| tap cycle, python_on | 1.57M/s | **1.62M/s** |
| transcribe, 60Hz mode | (never measured) | 27.8M frames/s |
| transcribe, precise mode | (never measured) | 4.3M frames/s |

Both columns were measured back-to-back in one sitting, which is the only
way these numbers mean anything: the same unmodified binary re-run hours
later reported 1.44M where it had reported 2.47M, purely from host load.
Compare within a sitting, never across sessions.

The tap path was already down to 0.40us/cycle after Session 4, so the few
percent there is all that was left in it; **the real wins in this pass are
the subprocess spawns that no longer happen**, which no benchmark here can
show because they were never CPU time in the first place — they were
tens of milliseconds of fork/exec plus a KWin round-trip, each.

- **"Ignore Puppetry" stopped polling kdotool.** It was spawning *two*
  subprocesses (`getactivewindow`, then `getwindowname`) every 50ms —
  ~40 a second — to ask KWin a question the GUI already knows the answer
  to: Qt tells it the instant its own window activates. `puppetry-transcribe`
  now accepts `focus 1` / `focus 0` commands on stdin (which it was
  already reading, for the EOF-means-stop signal), and
  `TranscriptionController` reports focus from `focusWindowChanged`.
  Faster *and* more accurate (no 50ms lag, no dependence on kdotool at
  all). The poll is kept as a fallback for running the helper standalone
  from a terminal, and switches itself off permanently the moment a focus
  command arrives. Unknown stdin commands are ignored on purpose, so a
  mismatched GUI/helper pair keeps working.
- **move_to halved its kdotool round-trips** via a cursor-position cache
  (`CursorCache` in `runtime.hpp`). An absolute move already ends by
  reading back where it actually landed, so that read now populates the
  cache and the *next* move_to starts from it instead of asking again: one
  round-trip per absolute move instead of two, and **none at all** for a
  move that's already on target (a transcribed "set positions" recording
  is full of those). Trust rules are pessimistic, because a stale cached
  position would send the cursor somewhere wrong: dropped when we emit any
  relative motion ourselves, when the user touches the real mouse
  (`dispatch.cpp` invalidates on real `REL_X`/`REL_Y`), and after 250ms
  regardless, since any application can warp the pointer whenever it
  likes. 8 of the new `test_core` checks cover the expiry/invalidation
  rules.
  - Careful subtlety, found while reviewing this: the "already on target,
    return early" shortcut applies **only to instant moves**. An eased
    move is also pacing the macro, and returning early would quietly make
    `move_mouse(x, y, move_to=True, time_=0.25)` take no time at all when
    the cursor happened to already be there.
- **LTO** (`CheckIPOSupported`, on by default in Release). Worth more than
  usual here because the hot paths are deliberately small functions in
  *separate* translation units — `kd()` -> `key_frame()` -> `write_all()`,
  `wait_fn()` -> `wait_until()` — which without cross-TU inlining stay
  real calls through the static library. Also `-DPUPPETRY_MARCH_NATIVE=ON`
  for a personal build (off by default: it produces a binary that only
  runs on the machine that built it, wrong for a package).
- **`run_kdotool()` was forking unsafely.** It built its `argv` vector
  *after* `fork()`, in the child — and the daemon is multithreaded, where
  only async-signal-safe calls are legal after a fork. A `push_back` there
  can block forever on the malloc lock if another thread held it at fork
  time, which would surface as this query mysteriously timing out and, for
  move_to, as the cursor going somewhere else entirely. argv is now built
  before the fork, the way `command_fn()` already did it. Latent rather
  than the reported bug, but the same shape as it.
- **No allocation per transcribed line.** Every emission now goes through
  one reusable buffer (`begin_line()`/`end_line()`) instead of building
  strings with `+`, and naming a key is an array index
  (`key_code_name_or_null()`) rather than a hash lookup returning a
  `std::string` by value. Byte-identical output — which the 28 exact-output
  transcriber checks are what made this safe to do at all.
- **Smaller ones**: easing resolved to an enum once per move instead of a
  string compare per step (120 steps/second); `std::pow(x, 2)` -> `x * x`;
  `move_rel_step()` returns early on a zero delta; `UinputDevice`'s frame
  counter is atomic (several macro threads share a device — it was a data
  race, benign in effect but real).

### Deliberately NOT done

- **Adaptive dead reckoning for move_to** — skipping the read-back
  entirely after a few consecutive exact landings, re-verifying every Nth
  move. It would make transcribed absolute-position playback nearly free
  (one kdotool spawn per N moves instead of one per move), and flat
  acceleration is exactly the condition that makes it sound. Left alone on
  purpose: it trades exactness for speed in the one place that just went
  wrong for the user, and there's no real KDE session here to test it
  against. This is the obvious next optimization if transcribed
  set_positions playback feels slow.
- **Making the virtual mouse an absolute (EV_ABS) device**, which would
  sidestep pointer acceleration entirely rather than configuring it away,
  and would let move_to skip kdotool completely. It's how remote-desktop
  tools do it. Much bigger change: absolute axes need the screen geometry
  baked into the device, relative moves would need their own virtual
  cursor, and libinput may reclassify the device as a tablet/touchscreen.
  Not attempted blind.
- Micro-optimizations under ~1% that would touch the grabbed-input path
  (the `ignore_mutex` read per motion event, the thread-safe-static guard
  in `is_key_code()`), which isn't worth the regression risk against a
  working system.

## Session 9 — transcription start/finish sounds, the real transcription inaccuracy, playback timing

Verified: full native suite green (`test_core` 55, `test_transcriber` 28 ->
**36** (+8), `test_timing` **47** (+1), `test_control_socket` 7,
`test_python_embed` 8, `test_native_vm` 5, `test_mixed_calls` 2),
`gui/test_app.py` **85/85** (+11).

### Transcription start/finish sounds (new)

Two optional sound files under Transcribe Inputs, played when
transcription starts and when it stops — however it stops (the button, the
hotkey, the abort key, a disconnected device), since the point is knowing
what happened when the hotkey was pressed from another window with the
status text out of sight. Each row has Choose… / ▶ (test) / ✕ (clear), and
paths persist as `transcribe_start_sound` / `transcribe_finish_sound`.

`gui/sound.py` plays them by spawning a player detached, **not** via
QtMultimedia: QSoundEffect is wav-only and QMediaPlayer needs the FFmpeg
backend plugins, which on NixOS are a separate output that may not be in
the wrapped environment — a missing one fails at runtime with nothing
useful to say. A binary on PATH can be checked for up front and reported
honestly (the section's help text names the player it found, or what to
install). Tries mpv, ffplay, mpg123 first (these handle mp3), then paplay
and aplay for wav/ogg/flac. A cue that can't play sets the status line and
is otherwise ignored — it must never get in the way of the thing it was
announcing.

### The occasional transcription inaccuracy: found, and it was real

**The keyboard and the mouse are separate file descriptors, and `poll()`
returns as soon as either is readable — but events were only sorted by
timestamp within a single wakeup.** So when the mouse's fd became readable
a fraction of a millisecond after the keyboard's, its events could carry an
*earlier* kernel timestamp and still be fed second. `TranscriberCore`
refuses to let its clock run backwards, so instead of a negative gap the
gap was silently clamped to zero: a click and a keypress half a
millisecond apart came out **in the wrong order, back-to-back**. Rare,
input-timing dependent, and exactly as subtle as "slightly inaccurate on
occasion". Reading at most 64 events per device per wakeup made it worse by
splitting bursts across wakeups.

Fixed with `EventReorderQueue` (`transcriber.hpp`): events are held for a
3ms window before being fed, so anything that was going to arrive out of
order has arrived, and then released in true timestamp order. Points worth
keeping in mind:

- **Recorded timing is completely unaffected.** Every gap still comes from
  the kernel timestamp the event was already carrying; the window only
  delays when a line is *written*, and the GUI already batches text into
  the editor every 50ms.
- The sort is **stable**, because one hardware frame emits several events
  sharing a timestamp and their order has to survive.
- `poll()`'s timeout is now shortened to the moment the oldest held event
  comes due, or a keystroke would sit in the queue for the full 250ms.
- Each ready device is now drained completely (the fds are non-blocking)
  rather than 64 events at a time.
- Everything still queued is flushed on exit — the last keystroke of a
  session is usually the one that ended it.
- 8 new `test_transcriber` checks, including the exact inversion above
  proven end-to-end: the click lands first with its real 500us gap intact.

### Playback consistency: what the measurement actually said

`test_timing` now reports the **distribution**, not just the median, which
is what "slightly inconsistent" is about. Current numbers on this sandbox:

```
wait(1ms) lateness over 2000 waits: p50 +0.1us, p99 ~80-330us, worst ~2-14ms
```

The median has been fine since Session 4. The tail is the problem.

**An adaptive spin margin was implemented, measured, and reverted.** The
theory was sound: the margin decides how early we stop sleeping and start
spinning, a fixed 100us is too small on a machine whose CPU takes a few
hundred microseconds to leave a deep idle state, and the p99 sitting right
around 100-170us looks exactly like that. Measuring the real overshoot and
growing the margin to match *should* have cut the tail. It did the
opposite — p99 went from ~120us to ~270us and the no-drift check started
failing — because most of the tail isn't wakeup latency at all, it's the
scheduler taking the CPU away. Spinning earlier can't prevent that, and the
extra spinning causes more preemption, lengthening the very tail it was
meant to cut. Filtering outliers out of the estimate didn't rescue it
either. The reasoning and the numbers are recorded in a comment above the
margin constants in `primitives.cpp` — **don't re-litigate it without a
before/after tail measurement**, which `test_timing` now prints.

What does address multi-millisecond preemption is scheduling priority, so
that's what shipped, **opt-in**: `"realtime_priority": true` in state.json
or Settings → Behavior → "Real-time priority (steadier playback timing)".
`main.cpp` takes SCHED_RR priority 1 — the gentlest real-time setting
there is: it round-robins with other RT tasks rather than monopolizing, and
the kernel's own RT throttling still guarantees normal processes CPU time,
so **a runaway macro can't lock the machine up**. Threads inherit their
creator's policy, so doing it once at startup covers macro threads, device
watchers and the control socket (which makes the abort hotkey *more*
responsive too). It needs `LimitRTPRIO`, which `module.nix` now grants —
granting the limit costs nothing on its own, since without the setting the
daemon never asks. Without permission it logs why and carries on. **Not
measurable in this sandbox** (no privileges), so its effect on a real
desktop is reasoned, not observed.

Also fixed while in there: the no-drift check was a single timed run with a
2.5% bound, which flaked whenever the host stole a timeslice — it now takes
the **median of 5 runs** like the neighbouring checks already did, keeping
the bound tight enough to still catch anchoring breaking without the suite
crying wolf. 8 consecutive runs clean, drift median pinned at 0.2001s.

### Still worth knowing

- If a recording was made with **set positions** on, playback timing is
  dominated by kdotool, not by any of the above: every motion line is an
  absolute move, and each one costs a subprocess round-trip. Session 8's
  cursor cache halved that and Session 8's "deliberately NOT done" section
  describes the adaptive dead reckoning that would mostly eliminate it.
  That remains the highest-value playback work left.

## Session 10 — transcription checkpoints, restart-transcription hotkey

Verified: full native suite green (`test_core` 55, `test_transcriber` 36 ->
**41** (+5), `test_python_embed` 8 -> **9** (+1), `test_native_vm` 5 ->
**7** (+2), `test_timing` 47, `test_control_socket` 7, `test_mixed_calls`
2), `gui/test_app.py` 85 -> **102** (+17).

### `checkpoint()` (new primitive) + checkpoint key

A genuine no-op: `checkpoint_fn()` in `primitives.hpp` does nothing at all,
registered identically in both execution paths (`native_vm.cpp`'s
`compile_op()`, `python_embed.cpp`'s `py_checkpoint`/`FC("checkpoint", ...)`)
so it behaves the same whether the macro's "Run as embedded Python" is on
or off. It exists purely as a text marker for the editor (below).

A new "Checkpoint key" under Transcribe Inputs, picked the same way as the
ping key (its own `DetectKey`, no listener needed — only the transcriber
process watches for it while actually recording). Pressing it while
transcribing inserts a literal `checkpoint()` line at that moment
(`transcriber.cpp`'s `feed()`, alongside the existing ping/hotkey/restart
filtering; `flush_motion()` runs first so pending mouse motion still lands
in front of it, same as any other keypress) — but, like the hotkey, the
keypress that triggers it never itself shows up in the transcript. Persists
as `transcribe_checkpoint_key`; passed to `puppetry-transcribe` via the new
`--checkpoint-key` flag; `TranscribeOptions::checkpoint_code` (-1 = none).

### "Clear macro before transcribing" is now checkpoint-aware

Previously this option always wiped the whole code box at the start of
every transcription session. Now (`editor_page.py`'s
`_apply_clear_before_transcribing()`, called from `_start_transcription()`
right before spawning the transcriber): it searches for the **last**
`checkpoint()` line in the current code. If none exists, behavior is
unchanged — the box is cleared entirely. If one exists, everything up to
and including that line is **kept**, only what comes after it is cleared,
and the cursor is moved to the end of the kept text so the new recording's
output lands right after the checkpoint instead of wherever the cursor
happened to be. `checkpoint()` does nothing at runtime either way — this
editor behavior is its only purpose. Drop one in by hand, or use the
checkpoint key while transcribing, to mark "keep everything before this
point, re-record everything after it."

### Restart-transcription hotkey (new, separate from the toggle hotkey)

A second, independent hotkey under Transcribe Inputs ("Restart hotkey"),
its own `DetectKey` picker, its own persisted pref
(`transcribe_restart_key`), its own `HotkeyListener`
(`_rearm_restart_key_listener()`/`_restart_key_pressed()`, armed on
`showEvent()` and torn down in `stop_threads()` exactly like the existing
toggle hotkey's listener, but as a distinct object — `_restart_key_listener`,
never `_hotkey_listener`). Pressing it only does anything while a
transcription is already running — a no-op from a standing stop, since
this hotkey never itself starts one — and while running it stops it and
immediately starts a fresh session in its place, same
clear-before-transcribing/checkpoint handling, same sounds, as opposed to
the toggle hotkey, which alternates start/stop from either state.
`_restart_transcription()` in `editor_page.py` checks
`self.transcriber.running()` first and returns early if it's not, otherwise
it's `stop()` then `_start_transcription()`. On the native side it's
filtered exactly like the
toggle hotkey (`transcriber.cpp`, `TranscribeOptions::restart_code`, CLI
flag `--restart-key`) — consumed, never transcribed, deliberately a
separate option field from `hotkey_code` per the requirement that the two
hotkeys be independently bindable.

### Verified vs. not verified

Everything above is covered by the native (`test_native_vm.cpp`,
`test_python_embed.cpp`, `test_transcriber.cpp`) and GUI
(`gui/test_app.py`) suites listed at the top of this section, run clean.
Not independently verified on real hardware in this sandbox: the actual
feel of pressing a real checkpoint/restart key mid-recording (no input
devices available here) — the logic paths are exercised directly instead
(feeding synthetic `RawEvent`s to `TranscriberCore`/`Transcriber` for the
native side, calling the handler methods directly for the GUI side).

## Build/run/environment

**Daemon:** `cd native && mkdir build && cd build && cmake .. && cmake
--build . -j && ctest --output-on-failure`. Produces `puppetry-daemon`
and `puppetry-transcribe` (+ `bench_cps`, a throughput benchmark that
isn't a ctest test). Release builds use LTO when the toolchain supports
it; `-DPUPPETRY_LTO=OFF` disables it and `-DPUPPETRY_MARCH_NATIVE=ON`
builds for this machine's cpu only (see Session 8). `test_timing` is labelled `timing`; packaged builds
run `ctest -LE timing` since a loaded build machine can deschedule it.
Needs a C++17 compiler, CMake, and `python3-embed` via pkg-config
(`python3-dev`/`python3-devel` depending on distro; NixOS gets this
through `pkgs.python3` in `module.nix`/`flake.nix` already).

**GUI:** `cd gui && QT_QPA_PLATFORM=offscreen python3 test_app.py` to
run the structural tests, or `python3 app.py` (needs a real display,
`pyside6` and `evdev` installed) to actually look at it — **do this
before trusting the GUI is done**, per "Verified vs. not verified"
above.

Config still lives at `~/.config/macro-daemon/` (state.json,
macros.json, profiles/*.json, aliases.json), same paths and format as
the previous Python daemon. The daemon is still a `systemd --user`
service (`macro-daemon.service`) — after a config change made outside
the GUI's own Save button, `systemctl --user restart macro-daemon` is
what actually applies it. Logs: `journalctl --user -u macro-daemon`.
