# Puppetry -- handoff notes for a new Claude chat

Max is continuing work on Puppetry (a NixOS keyboard/mouse macro daemon +
GTK4 editor) in a new chat because the previous one was running out of
context. This file is everything you need to pick up where it left off.
The full repo as of this handoff is included alongside this file.

## Architecture, in brief

- `macro_daemon.py` -- background systemd --user service. Reads the real
  keyboard/mouse read-only via evdev, matches key combos, and fires
  compiled Python macro code that writes synthetic input through two
  `UInput` virtual devices (`ui_keyboard`, `ui_mouse`). Macro code is
  arbitrary Python compiled via `compile_macro()` with a namespace of
  primitives (`kd`, `ku`, `tap`, `wait`, `speed`, `ignore`, `move_mouse`,
  `type`, `wheel`, plus bare `KEY_*`/`BTN_*` constants).
- `macro_gui.py` -- GTK4 editor (`MainWindow` = profile/macro list,
  `MacroEditorWindow` = per-macro editor with live input transcription).
- `module.nix` / `flake.nix` -- the standalone NixOS flake Max's friend
  consumes as `puppetry.nixosModules.default`.
- Repo: `github.com/mr-tinkle-winkle/puppetry`. Max's own machine
  consumes it as a flake input (`services.puppetry.enable = true; user =
  "mrtw";`) rather than local files -- **his real edit workflow is: edit
  files in `~/puppetry`, `push-puppetry "message"` (his own bash function
  wrapping git add/commit/push), then `flake-update puppetry &&
  sudo nixos-rebuild-flaked` to pull it into his system, then restart the
  daemon (Save button in the main window, or `systemctl --user restart
  macro-daemon`) if the change touched `macro_daemon.py`.**
- Max is on **bash**, not fish, despite fish being visible in some old
  history/aliases.

## Open problems, as of this handoff (Max's own words, verbatim)

Reported together in one message; **items 1 and 2 were fixed and
deployed in this same session** (see "What was just fixed" below).
**Items 3, 4, and 5 are UNRESOLVED** -- do not assume they're fixed.
Item 3 in particular needs real investigation, not guessing.

1. ~~The Paned drag handle in the macro editor doesn't resize the code
   box horizontally; code box should default to ~1.5-2x wider.~~ FIXED
   below -- **but not yet confirmed working by Max**, since context ran
   out before he could test it.
2. ~~Auto-enable "Simplified Variable Names" for new macros (opt-out
   instead of opt-in).~~ FIXED below (one-line default flip, high
   confidence this is correct).
3. **UNRESOLVED -- real bug, needs investigation, NOT started.** Mouse
   inputs (both movement and clicks) triggered *from a macro*
   (`kd(BTN_LEFT)`, `move_mouse(...)`, etc.) stopped working. Max says
   this is an OLD macro (predates even the abort-hotkey update), and
   that it worked before recent changes and doesn't now. Keyboard-only
   macros were NOT reported broken -- this appears mouse-specific. See
   "Debugging notes on problem #3" below for what was checked so far
   (not much -- I'd only confirmed `kd`/`ku`/`tap` look unchanged in
   logic before running low on context; `move_mouse()`'s full body and
   `_move_rel_step()` were NOT yet re-read line by line).
4. **UNRESOLVED, NOT touched this session.** Text-render ghosting bug in
   the code editor -- confirmed still present via screenshot (top/bottom
   slivers of glyphs from a line above persist after the line below
   shifted down). The earlier `queue_draw()`/`scroll_mark_onscreen()`
   fix in `_on_transcribe_line` evidently did NOT fully fix it. No
   further attempt was made this session -- see the hypothesis/next-steps
   note below.
5. **UNRESOLVED, NOT touched this session.** `ignore()` mid-action
   doesn't release currently-held buttons/keys the way it should. This
   was *supposed* to already be handled -- `_apply_grab_state()` calls
   `_release_held_from(kind)` right after a successful `.grab()`, meant
   to synthetically release anything in the global `held` set for that
   device kind. Max says it's still not working. Needs re-verification:
   is `_release_held_from` actually being reached? Is `held` populated
   correctly at that moment? Does the synthetic release actually land
   before/after the real grab in a way that matters?

## Debugging notes on problem #3 (the important one)

Before running out of context, I re-read `kd()`, `ku()`, `tap()`, and
`_move_rel_step()`'s signature fresh (not from memory of earlier diffs)
specifically looking for a regression. **Nothing obviously wrong was
found in what I looked at** -- `kd`/`ku` still route through
`_device_for_code()` and write/syn correctly; the only change from
before was adding `_synth_held` bookkeeping (add/discard in a lock),
which shouldn't affect device writes at all. **I did NOT get to fully
re-read `move_mouse()`'s body or `_move_rel_step()`'s body before
running low on context -- do that first, it's the most likely remaining
place a mistake could hide.**

Things that changed recently that touch mouse-adjacent code, roughly in
order, any of which is a plausible place to look:

1. `watch_device(path)` became `watch_device(path, kind)` and gained
   forwarding logic gated on `_grab_state.get("mouse")` /
   `_ignore_flags[...]`. This only activates once a device is actually
   grabbed, so it *shouldn't* affect a macro that never calls
   `ignore()` -- but Max's test list from the previous round explicitly
   told him to test `ignore("mouse_buttons")` and
   `ignore("mouse_movement")`. **First thing to ask Max: did he run
   those ignore tests before or after noticing mouse macros broke?** If
   after, check whether `_grab_state["mouse"]` could have gotten stuck
   `True` somehow (it shouldn't survive a daemon restart -- `_grab_state`
   is a plain in-memory module dict, resets on process start -- but
   confirm this isn't wrong, and confirm the daemon was *actually*
   restarted between his ignore test and his mouse-macro test).
2. `move_mouse()` gained `scaled_time = time_ * _current_speed_multiplier()`
   and `_check_abort()` calls inside the stepped-easing loop. Shouldn't
   affect a macro not using `speed()`/aborting, but **read the full
   current function body fresh, don't trust this summary.**
3. `abort_all()` now force-clears `_ignore_flags` and ungrabs devices as
   a safety net. Shouldn't run at all unless the abort hotkey fired.

**Concrete next steps, in priority order:**
1. Ask Max to run the broken mouse macro and immediately get
   `journalctl --user -u macro-daemon -n 50 --no-pager` -- look for any
   traceback (an exception inside a macro thread would print via
   Python's default `threading.excepthook`, which should show up here).
2. Confirm which specific primitive is failing -- does `kd(BTN_LEFT)`
   alone (in a trivial throwaway macro, no other logic) fail? Does
   `move_mouse(100, 0)` alone fail? Narrow it to one primitive before
   reading more code.
3. Confirm `state.json`'s `mouse_name`/`mouse_path` are still what they
   should be (`grep -A2 '"mouse_name"' ~/.config/macro-daemon/state.json`)
   -- rule out a device-detection regression, not a code regression.
4. Only after those three, re-read `move_mouse()`/`_move_rel_step()` in
   full.

## What was just fixed in this session (deployed, awaiting Max's test)

- **Paned drag bug (#1) -- root cause found, fix applied, NOT YET
  CONFIRMED by Max.** The left pane's content included several
  `Gtk.Box(HORIZONTAL)` rows of 2-3 checkboxes with long labels (e.g.
  "Ignore Keyboard Input (except Abort)"), which don't wrap in GTK4 --
  their combined natural width was almost certainly pinning the pane's
  minimum size well above where Max was trying to drag it to, making
  the divider *look* broken when it was actually just clamped by
  content it couldn't shrink below. Fixed by switching those three rows
  (`transcribe_check_row`, `raw_alt_row`, `ignore_row`) from horizontal
  to vertical stacking, plus `resize_start_child(False)` /
  `resize_end_child(True)` on the Paned itself (the standard "fixed
  sidebar, growing main content" idiom -- having both sides
  `resize=True`, the previous setting, is a documented rough edge in
  GTK's Paned that can make dragging feel unresponsive). Default window
  width increased to 1600 (from 1120) and the paned's initial position
  set to 400, giving the code side roughly 2x the settings side by
  default. **If dragging still doesn't work after this, the wide-content
  theory was wrong (or incomplete) and needs a different diagnosis --
  don't assume this fix is correct just because the reasoning sounds
  plausible.**
- **Simplified names default (#2)**: `_blank_macro()`'s
  `"simplified_names"` default flipped `False` -> `True`. Simple,
  high-confidence, low-risk change.

**Deployment reminder for whatever this new chat produces**: copy
changed files into `~/puppetry`, `push-puppetry "message"`, then on
Max's own machine `cd /etc/nixos && flake-update puppetry && sudo
nixos-rebuild-flaked`, then restart the daemon if `macro_daemon.py`
changed (Save button in the main window, or `systemctl --user restart
macro-daemon`) -- GUI-only changes just need relaunching `puppetry`.

## Known working / already-tested-and-confirmed-good features

(Note: the screenshot Max sent for item #4 isn't included in this
handoff -- only its description above. If you need visual confirmation,
ask Max to resend it.)


(So the new chat doesn't waste time re-litigating these.) Device
auto-detect (with virtual-device exclusion), `move_to`/ping-based
absolute mouse positioning with closed-loop drift correction, `speed()`
multiplier, `wait()`/`move_mouse()` abort-interruptibility, the abort
hotkey itself (stops macros + releases `_synth_held` keys), raw-mode
mouse transcription at a configurable tick rate, Same Starting Mouse
Position / Set Mouse Positions (mutually exclusive), transcription
settings persistence, taskbar/launcher/titlebar icon (all three
required separate fixes: desktop-entry placement, GApplication id
validity, and per-window `set_icon_name()`), and disabled macros being
callable-by-name from other macros' code (this was already true by
construction, no code change was ever needed for it).
