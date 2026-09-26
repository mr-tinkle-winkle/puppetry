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

## Session 3 (this session) — the Qt/C++ rewrite, `actAs`, per-key `ignore`

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

## Known issues / not yet resolved

- `ignore()` does not reliably release a keyboard key that was already
  held down at the moment its grab kicked in. Deprioritized by
  request. `trigger_edge = "up"` sidesteps this for a macro's own
  combo keys specifically, not the general case.
- A text-rendering ghosting bug in the code editor during live
  transcribe mode. Deprioritized by request as a minor front-end
  concern — see "Transcription accuracy" under Planned work, though,
  since a related-but-distinct transcription concern *is* now queued.
- This and the prior session's GUI-side changes (lock toggle,
  trigger-edge dropdowns, Record Time spinner, editor zoom, JSON
  control-socket wiring on the GUI side) have not been exercised
  against a real running GTK4 app — only syntax-checked and
  structurally reviewed. Daemon-side changes have been exercised with
  real (non-GTK) test harnesses in a sandboxed environment.
- The `command()` PATH/stderr fix (this session) has not been
  reconfirmed against the exact originally-reported failure — see
  above.

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
  `ZoomablePlainTextEdit` in `gui/app.py`) and Ctrl+0 to reset it.
- A real desktop verification pass on the Qt GUI (see "Verified vs.
  not verified" above) — this is the single highest-priority item,
  since nothing about how it actually looks has been checked yet.
- `native/module.nix`/`native/flake.nix` haven't been evaluated against
  a real `nix build` (no `nix` binary in the sandbox this was written
  in) — get a real build working before trusting the derivations.
- Root-cause the daemon's actual CPS ceiling now that the hot path is
  native (uinput `write()` syscall cost vs. kernel/evdev vs. compositor
  polling) — this was deliberately NOT investigated before the C++
  rewrite (see Session 3 above), so it's now worth doing empirically
  against the real implementation.
- Increase the transcriber's recorded decimal precision for
  timing/position values (the originally-suspected cause of tight-3D-
  platforming timing issues) — not yet touched; there is no
  transcriber in this codebase yet at all, native or GUI-side, so this
  is still fully unbuilt work, not a partial one.
- `Act As` and per-key `ignore` are DONE this session (see Session 3
  above) -- keeping this line only so a future skim of this list
  doesn't wonder where they went.

## Architecture / file map

- `native/` — the daemon (C++17, CMake). See `native/src/*.hpp` for
  the module breakdown (`runtime.hpp` is the shared mutable state,
  `primitives.hpp`/`.cpp` the macro-callable primitives including
  `actAs`, `dispatch.hpp`/`.cpp` the combo-matching + evdev watch loop,
  `macro.hpp`/`.cpp` the repeat-mode/trigger-edge runtime,
  `python_embed.hpp`/`.cpp` the CPython-backed macro path,
  `native_vm.hpp`/`.cpp` the primitives-only fast path,
  `control_socket.hpp`/`.cpp` the daemon's control socket,
  `config.hpp`/`.cpp` the on-disk JSON I/O, `main.cpp` the entry
  point). `native/tools/gen_keycodes.py` generates the key/button name
  table at build time. `native/tests/` holds the test suite described
  under "Verified vs. not verified" above — `cmake --build . && ctest
  --output-on-failure` from a `native/build/` directory runs all of it.
- `gui/` — the Qt (PySide6) editor:
  - `gui/app.py` — the whole app (`MainWindow`, macro list/editor
    pages, CLI entry point). Read its own module docstring for
    this-session's exact feature scope.
  - `gui/puppetry_config.py` — shared on-disk config I/O + the daemon
    control-socket client.
  - `gui/combo_recorder.py` — the combo recorder (flagged above as a
    fresh implementation, not a line-for-line port).
  - `gui/test_app.py` — offscreen structural tests (`QT_QPA_PLATFORM=offscreen
    python3 gui/test_app.py`).
  - `gui/ui_kit/` — the shared PySide6 theming kit (copy, not a shared
    package — see the guide for why). `gui/UI_THEMING_GUIDE.md` is its
    full spec; `gui/ui_kit_test_kit.py` is its own 26-check offscreen
    suite, unmodified and still passing.
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

## Build/run/environment

**Daemon:** `cd native && mkdir build && cd build && cmake .. && cmake
--build . -j && ctest --output-on-failure`. Produces `puppetry-daemon`.
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
