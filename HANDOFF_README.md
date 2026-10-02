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

## Start here — current state (Session 26)

**What it is.** A NixOS input-macro system: a C++17 daemon (evdev in,
`uinput` out; control socket; embedded CPython *or* a built-in interpreter
per macro) plus a PySide6 editor. Macros are written as blocks
(puzzle-piece canvas) or text; both views share one source of truth, the
macro's `code` string. Categories group and enable/disable macros
separately from profiles.

**Built and covered by tests.**
- Daemon: primitives `tap kd ku combo type move_mouse wheel command wait
  speed checkpoint ignore ignore_keys actAs waitForPress
  waitForReactivation getMousePosition getButtonsHeld`; native interpreter
  (variables, control flow, functions, lists) checked differentially
  against embedded Python; custom blocks registered as combo-less macros.
- GUI: macro list with categories and profile switcher; editor with
  Blocks | Text views; custom-block dialog; batched autosave (first save
  immediate, then one save + one daemon restart after 1 s of quiet);
  wheel-proof dropdowns app-wide; color scheme (purple = neither input nor
  output, orange = input, blue = output, green = enabled, red = disabled).
- **Dictionary page (new).** Nav entry between Input Visualizer and
  Settings. Sections: Commands (parsed from `reference.DICTIONARY_TEXT`, so
  prose, palette tooltips and page share one source; signatures colored by
  category), Blocks and code (`reference.LANGUAGE_ENTRIES`), Custom blocks
  (read from `custom_blocks.json` each time the page is shown), Key and
  button names (the daemon's `--dump-names` tables), Notes. One search box
  filters every section by name and description. The editor's two
  collapsible reference panels were replaced by an "Open the Dictionary"
  button. `ignore(target)` in the prose was corrected to `ignore(what)`.
- **Input Visualizer (extended).** The capture area now draws an ANSI
  keyboard (main block, navigation cluster, arrows) and a mouse (L, R,
  wheel, two side buttons); whatever is held lights up in input-orange, the
  wheel flashes an arrow, the last movement delta is printed. Below it, a
  "Macro equivalent" readout turns every keyboard/mouse event into code:
  `tap(KEY_A)`, `combo(KEY_LEFTCTRL, KEY_C)`, `wheel(2)`,
  `move_mouse(dx, dy, time_=..., easing="linear")`, `wait(...)` between
  actions, exact `kd/ku` for anything that does not fit (key held during
  mouse movement, out-of-order release, key still down). Toggles: combine
  into tap/combo/wheel, include waits; Copy, Clear. Logic lives in
  `gui/input_transcript.py` (pure Python, unit-tested); the hot path only
  appends tuples, formatting runs on the page's 50 ms timer.
  Key names come from the kernel scan code (`nativeScanCode() - 8`, which
  separates left/right modifiers) with a Qt-key fallback.

- **Dictionary (Session 17).** Text is 3x the app's size
  (`dictionary_page.TEXT_SCALE`). Every entry is collapsed to its colored
  signature and expands on click; Expand all / Collapse all; a search that
  matches only a description opens that entry, and clearing the search
  closes what it opened. "Key and button names" is now the visualizer's own
  keyboard + mouse (`KeyNameMap`, a `KbmView`) with the short name drawn big
  and the `KEY_`/`BTN_` name small, hover tooltips listing every alias, and
  search lighting matching keys up. Keys not on the picture are listed under
  "Other key names".
- **Visualizer (Session 17).** Held keys show a hold timer
  (`seconds.milliseconds`, `m:ss.mmm` past a minute) in place of their
  label. A curved arrow under the mouse shows the last movement burst (a
  burst ends after 150 ms of stillness): a cubic Bezier through the
  positions at 1/3 and 2/3 of the burst's *duration*, so left-then-up
  bends the end up and up-then-left bends the start up; length grows with
  the square root of the distance.
- **OBS & replay overlay (new, Session 17).** Section at the bottom of the
  Input Visualizer page (`gui/overlay_settings.py`). Data flow:

      daemon (event_stream.cpp) --$XDG_RUNTIME_DIR/puppetry/events.sock-->
        puppetry-overlay serve (overlay_server.py, stdlib only)
          +-- HTTP + Server-Sent Events pages: full :17380, simple :17381, mouse :17382
          +-- input_buffer.jsonl (layered replay buffer, tmpfs) + overlay_status.json
      puppetry-overlay render/apply/composite (overlay_render.py, Qt offscreen -> ffmpeg)

  - The daemon always runs the event stream (one relaxed atomic load per
    event when nobody listens). Real events carry the kernel's evdev
    timestamp (CLOCK_REALTIME); macro output is marked `m` via a hook in
    `UinputDevice::key_frame/rel_frame/wheel_frame`. It starts the helper
    (`PUPPETRY_OVERLAY_CMD`, set by module.nix; else `puppetry-overlay` on
    PATH) only when `overlay.json` enables a page or the replay buffer,
    restarts it if it crashes, and stops respawning on exit 0.
  - One drawing model everywhere: `kbm_layout.py` (geometry in key units,
    `KbmState`, arrow math, timers; no Qt), `kbm_paint.py` (QPainter: app,
    Dictionary, previews, renderer), `overlay_web/kbm.js` + `common.js`
    (browser twin). A test runs the JS arrow/timer math under node and
    compares it with Python exactly.
  - Style: `overlay_config.STYLE_SCHEMA` / `SIMPLE_SCHEMA` / `MOUSE_SCHEMA`
    drive defaults, the Customize forms and what pages receive; the helper
    re-reads `overlay.json` on change, so style edits reach OBS live. Pages
    or ports toggled -> the GUI restarts the daemon after 1 s of quiet.
  - OBS: `obs_client.py` (stdlib obs-websocket v5 client with auth). Replay
    length = the `replay_buffer`-kind output's `max_time_sec`, else profile
    `RecRBTime`; polled every 30 s; fallback setting otherwise. "Add to OBS"
    creates or updates Browser Sources in the current program scene, sized to
    each page.
  - Replay file format, CLI contract and timing model: see the afterglow
    prep package (`FORMAT.md`, copied to `docs/overlay_interface.md`), mirrored in `gui/overlay_render.py` and
    `gui/overlay_server.py` docstrings.

- **Session 18: controllers, pieces, source colors.**
  - *Controllers* (daemon): a third, optional, hot-pluggable device
    (`find_best_controller`: BTN_SOUTH + ABS_X; re-scanned every 3 s,
    re-found after unplugging; state.json `controller_path/name`,
    `watch_controller`). Its buttons go through `handle_key_event`, so they
    work in combos, `waitForPress`, `getButtonsHeld`; it's read, never
    grabbed. Axes are normalized per device (`normalize_abs_range`: sticks
    -1..1, triggers 0..1, hat -1/0/1) into `Runtime::axes` and streamed as
    `a` lines (only changes >= 0.004). New primitives on both paths:
    `getAxis(axis)` and `axis(axis, value, time_=0)` (names LX LY RX RY LT
    RT DPAD_X DPAD_Y). Output goes to an opt-in virtual controller
    (`virtual_controller` in state.json, Settings > Devices;
    `UinputDevice::create_gamepad`, Xbox-style layout), and controller
    buttons in tap/kd/ku route there. Without it they raise a clear error.
    Abort releases its buttons and recenters its axes.
  - *GUI*: Settings > Devices gets a Controller row (Detect), "Watch a game
    controller" and "Virtual controller". The combo recorder listens to
    controllers too. Blocks: "move controller axis" (output) and a
    "controller axis" reporter (dropdown), round-tripping
    `getAxis("LX")`. Dictionary entries for both plus controller button
    names.
  - *Pieces*: `kbm_layout.build_piece("full"|"keyboard"|"mouse"|"controller")`;
    the gamepad is drawn by the same painter (Qt + JS): triggers fill with
    their axis, sticks' knobs follow theirs, face labels Xbox / PlayStation
    / Nintendo. OBS pages: `full` (or `keyboard` + `mouse` with "separate
    pieces"), `controller`, `simple`, `movement` (the arrow-only page,
    formerly `mouse`; `mouse_style` migrates to `movement_style`). Default
    ports 17380 / 17383 / 17384 / 17385 / 17381 / 17382.
  - *Source colors*: `KbmState` tracks the source of keys, motion bursts,
    wheel and axes. Real input is drawn in `pressed_color` (input orange),
    macro output in `macro_color` (output blue), on by default
    (`show_macro_output`). The simple list colors each part; the in-app
    visualizer now reads the daemon's stream when it's there (with a
    window-events fallback) and shows the controller when one is used.
  - *afterglow flow*: `render` per piece, then `align` (stream-copy cut to
    the clip), `layer` (burn several pieces at fractional placements).
    Pieces are transparent qtrle; VP9 alpha needs the libvpx decoder
    (documented). The prep package (v2) describes the sidecar design:
    nothing burned in until export; previewer/editor toggle, move and
    resize via mpv `lavfi-complex`.

- **Session 19: scene of elements, movement views, Steam devices.**
  - *Scene* (`overlay.json` `scene.elements`, edited in the visualizer):
    each element is `{id, type, x, y, scale}` in key units plus
    `layout` (keyboard presets `full` / `tkl` (80%) / `60` / `half` (left
    half, gaming)), `look` (mouse `classic` / `gaming` (extra side buttons)
    / `minimal` / `buttons` (buttons only)), or `size` + `center` (movement
    views). Types: keyboard, mouse, controller, comet, mousepad, joystick;
    any number, any arrangement. `kbm_layout.build_scene(scene, only=id)`
    builds the whole picture or one element; items carry `el` and `fs`
    (element scale). `DEFAULT_SCENE` = TKL keyboard, classic mouse, a
    tail-centered comet under the mouse. Old configs migrate (`split` ->
    `element_sources`, old controller page -> a controller element).
  - *Edit layout* (visualizer page): drag to move (quarter-key snap),
    corner handle to resize (0.3x-4x), right-click for layout / look /
    centering / remove; "+ Add element", "Reset layout". A controller
    button seen on the stream shows a hint to add the controller here; the
    old "show controller" checkbox is gone.
  - *Movement views* (replace the curved arrow; `kbm_layout.motion_frame`,
    mirrored exactly in `common.js` `motionFrame` and checked under node):
    a square, invisible-background view of the last `trail_seconds` of
    pointer motion. Trail width shrinks toward older points, alpha falls in
    the oldest quarter; the dot is wider than the trail. *Comet*: centered
    on the tail (oldest point, default) or the head. *Mousepad*: a fixed pad
    whose origin re-centers after `recenter_s` of stillness. *Joystick*: the
    dot shows velocity (tanh-mapped by `joystick_speed`). View scale: the
    pad side is `pad_fraction`% (80) of `screen_height` (the primary
    screen's height, auto-filled); `auto_zoom` (default on) zooms out at
    once when the trail would leave the view and eases back in (tau 0.6 s);
    off = crop mode (drop the points that would go off-view, fade faster).
    Clicks: left = ring out, right = ring in, middle = two 60-degree arcs up
    and down, side = a short arc toward back/forward, held button = a ring
    on the dot; scroll = chevrons stacking above/below. Each trail segment
    mixes input/output color by how much macro motion it contains.
  - *Pages*: `full` at `/` draws the scene; with `element_sources` every
    element is also served at `/el/<id>` (events filtered with `?el=`), and
    "Add to OBS" creates one source per element ("Puppetry: <id>").
    `movement` (port 17382) shows one view chosen by its `motion` style.
    The per-piece ports of Session 18 are gone. Renderer modes:
    `full`, `simple`, `movement`, `el:<id>` (or a bare id / type).
  - *Steam Controller / Steam Deck*: in desktop mode Steam turns the pad
    into its own virtual keyboard and mouse (and a gamepad only in game),
    so `find_best_controller` never saw it. The daemon now also watches
    every Valve (vendor 0x28de) input node read-only as an *extra*
    (`watch_steam_devices`, default on), optionally every input device
    (`watch_all_devices`), rescanning every 3 s; a claim registry keeps the
    controller and extras watchers from opening one node twice. Extras feed
    the stream and the visualizer and are never grabbed.
    `puppetry-daemon --list-devices` prints every node with vendor, kind and
    whether Puppetry would watch it. Not confirmed on the hardware.

- **Session 20: Edit sidebar, follows head, invert side rings.**
  - `gui/element_panel.py` `ElementPanel`: element list (a touching
    vertical stack of `SegmentButton`s) plus the selected element's options
    as touching toggle rows: keyboard size Full / 80% / 60% / Half, mouse
    look, movement style Comet / Mousepad / Joystick, "Comet follows" Head /
    Tail, "Invert side button rings", scale, Remove; "+ Add element" and
    "Reset layout" moved into it. Shown beside the capture area while
    editing (fixed 340 px) and at the top of the full page's Customize
    sidebar. It edits the scene dict it was given in place; the capture
    area and panel share one dict (`VisualizerPage._panel_changed`,
    `_area_scene_changed`, a `_from_panel` guard so a spin box isn't
    rebuilt mid-edit); `OverlaySection.on_scene_edited` pushes Customize
    edits back to the area. The right-click menu has the same options.
  - Customize sidebar: fixed width `SIDEBAR_WIDTH` 560 (or wider if the form
    needs it), horizontal scrolling off, labels wrap.
  - Comet default is now "follows head" (`center: "head"`) everywhere
    (scene default, new elements, movement page, painters, JS).
    `overlay_config.DEFAULTS_REV` = 1: configs saved before it get their
    comets and movement page switched from tail to head once; a later
    choice of tail is kept.
  - Invert side button rings: back (BTN_SIDE) arcs left and forward
    (BTN_EXTRA) right; inverted swaps them. Per element (`invert_side`,
    carried on the motion item and merged into the style as
    `invert_side_rings` by both painters) and on the movement page's style.
    Python/JS parity cases cover it.

- **Session 21: movement fixes, Mousepad default, 60% arrows, fonts.**
  - Mousepad auto zoom now fits the whole trail (every point in the window
    plus the head, measured from the pad origin), not only the head; with
    auto zoom off, points that left the pad are dropped oldest first.
  - Comet never zooms: it is always drawn at the true scale (pad_fraction %
    of screen_height), so trail length shows speed; what would leave the
    view is dropped oldest first (both centers). `auto_zoom` is labelled
    as a Mousepad option. Python and `common.js` changed together; the
    parity cases cover both.
  - Mousepad is the default movement view (DEFAULT_SCENE, the movement
    page's `motion`, first in `MOTION_TYPES`). Movement elements get ids
    `movement`, `movement2`, ... (`new_element_id`) so a style switch keeps
    the id and its `/el/<id>` URL. `DEFAULTS_REV` = 2 turns the old default
    comet (id `comet`) into a mousepad with id `movement`, and the movement
    page's comet into mousepad, once.
  - 60% keyboard option `arrows` (element field; sidebar checkbox and
    right-click item when the size is 60%): the common arrow variant,
    1.75u right Shift, Up, `/` on the shift row; Alt, Menu, Left, Down,
    Right after Space. Rows stay 15u.
  - Fonts: 21 Google fonts (Fontsource latin subsets, WOFF converted to TTF
    with fontTools, 400 and 700 where they exist; licenses in
    `overlay_web/fonts/licenses/`, OFL 1.1 or Apache 2.0) cataloged in
    `gui/font_catalog.py` by category (System, Comic, Serif, Sans,
    Typewriter, Script, Novelty). Qt: `register_qt_fonts()` in
    `MainWindow` and the renderer. Pages: the server serves `/fonts.css`
    (generated `@font-face`) and whitelisted `/fonts/<file>.ttf`; pages
    link the CSS, `fontStack()` quotes names for canvas, and
    `document.fonts` load events redraw. `FontPicker` (schema type `font`):
    each name drawn in its own font, category on the right in the UI
    font, separators between categories, popup sized so nothing
    truncates, and "Other installed font..." for system fonts.

- **Session 22: Mousepad zooms back in; the page shows the overlay's look.**
  - Mousepad auto zoom: zooming out is immediate and stamps `grow_t`; once
    `unzoom_s` (default 0.6 s, new Mousepad option) passes without another
    zoom-out, the origin eases (tau 0.35 s) to the middle of the trail's
    bounding box and the zoom eases up to the most the trail allows,
    reaching 1.0 when it fits -- no rest needed (before, the zoom was
    measured from a fixed origin, so steady movement kept it zoomed out
    until the idle re-center). Python and `common.js` match; parity cases
    and a swipe-then-circle test cover it.
  - The Input Visualizer page's picture draws with the overlay style
    (`CpsArea.set_overlay_style(cfg["style"])`, padding 0), refreshed by
    `OverlaySection.on_style_changed` on every Customize edit, so it shows
    what OBS and afterglow get. The Dictionary keeps the app-themed style.

- **Session 23: new logo.** A wooden puppet hand on strings (orange, blue
  and purple fingers). `assets/puppetry_logo.png` is the original artwork;
  `assets/puppetry_small_logo.png` is it trimmed to the hand and centered on
  a 1024x1024 transparent square for the icon sizes. `MainWindow` falls back
  to that file when the `puppetry` theme icon isn't installed.

- **Session 24: daemon no longer exits into a restart loop.** Reported
  after a reboot: `restart counter is at 5`, macros and the visualizer
  dead. The log wasn't available when this was written, so the cause is
  unconfirmed; every startup exit path that a reboot can trigger now
  waits instead of exiting:
  - `/dev/uinput` not ready: retries every 2 s (`PUPPETRY_NO_WAIT=1`
    restores the old exit); `UinputDevice::create` closes its fd on
    failure so `ok()` is accurate.
  - Keyboard/mouse not found: keeps looking every 2 s.
  - Keyboard/mouse lost while running (`watch_device` returning): the
    `keep_watching` loop re-resolves and reopens (same name preferred for
    ~10 s before accepting another device); `watch_device` clears
    `watched_keyboard/mouse` and its grab flag on the way out. Previously
    `main` returned here while detached threads still used `rt`.
  - Device choice: `device_fits(kind, path)` -- a remembered path is reused
    only if it still is that kind of device (eventN numbers move between
    boots); among same-named nodes the keyboard pick is the one with the
    most letter keys and the mouse pick the one with REL_X/REL_Y.
  - Root cause (found from the device list): keyboard detection counted
    `for (c = KEY_A; c <= KEY_Z; ++c)`, but evdev numbers keys along the
    QWERTY rows, so that range (30..44) holds only 10 letters and the
    "20+ letters" test could never pass. Auto-detect had never worked; only
    the remembered path did. A new device enumerating first (a Steam
    Controller Puck) shifted the keyboard's eventN, the remembered path
    stopped matching, and detection fell through to the broken test. Fixed
    with `letter_key_codes()` (the 26 letters) in `find_best_keyboard`,
    `device_fits` and `describe_device`; `test_core` checks it.
  - Auto-detect now ranks real hardware first (`device_rank_penalty`: Valve
    vendor 0x28de = 2, BUS_VIRTUAL = 1): Steam Controller "Puck Keyboard"
    nodes and virtual devices (StreamController's uinput device) declare
    letter keys too and enumerate earlier.
  - Earlier note, kept for the diagnostics: `list_input_devices()` silently drops nodes it
    can't open, so a permission problem on the keyboard node looks like
    "no keyboard". Diagnostics added: `unreadable_input_devices()`;
    `--list-devices` includes unreadable nodes with the error; while the
    keyboard or mouse is missing, the log lists every visible node with its
    kind and every node it can't open (on the first try and again after
    ~30 s).

- **Session 25: afterglow's `mouse` piece shows movement again.** Since
  movement views became their own layout elements (Session 19), render mode
  `mouse` meant the mouse element alone, so afterglow clips captured with
  `keyboard` + `mouse` lost the movement. `FrameMaker` now builds `mouse`
  (by type) as the mouse plus every movement element, in their layout
  arrangement (`build_scene(only=[ids])`); `el:mouse` is still the mouse
  alone. Also: the movement views keep `half = box/2 - dot_r*1.75 - 1` so
  the held-button ring around the dot isn't clipped at the view's edge
  (Python and JS). Prep package v6 documents it.

- **Session 26: blocking the visualizer, ignored apps, on-screen overlay,
  viewers-only share window, keybinds.** (Built across two sessions; the
  second found the first's work on disk, reviewed it and completed it.)
  - *Blocking* (`native/src/privacy.{hpp,cpp}`): `EventStream::set_paused`
    -- while paused nothing is published, one gate for every consumer (OBS
    pages, replay buffer, in-app picture, on-screen overlay). Wire format:
    `s <t> p 1 0` / `s <t> p 0 0`; the hello of a client connecting while
    paused is empty plus that line. Sources: the manual toggle (control
    socket `{"cmd": "VISUALIZER", "state": "toggle"|"on"|"off"|"status"}`;
    kept in `$XDG_RUNTIME_DIR/puppetry/visualizer_blocked` so a daemon
    restart doesn't unblock) and ignored apps (overlay.json `ignored_apps`:
    `{"match": "class"|"title", "value", "when": "focused"|"open"}`,
    case-insensitive substring; `run_privacy_watch` polls every 0.5 s via
    kdotool -- active window class + title, and `search --class/--name`
    with `icase_substring_regex` for "open"; fails open when kdotool can't
    answer, reported as `watching: false`). State for the GUI:
    `$XDG_RUNTIME_DIR/puppetry/privacy.json`.
  - Consumers: the hub releases everything held on a block (ku / zero
    axes into pages and the replay buffer) and writes its status at once;
    `gui/stream_client.py` (shared by the in-app picture and the on-screen
    overlay) clears its `KbmState`.
  - *On-screen overlay* (`gui/screen_overlay.py`): one long-lived process,
    socket `$XDG_RUNTIME_DIR/puppetry/screen-overlay.sock` (show / hide /
    toggle / quit / status -> JSON). `puppetry-overlay screen
    show|hide|toggle|stop|status` starts it when needed through
    `systemd-run --user --unit=puppetry-screen-overlay` (outside the
    daemon's cgroup, with the session's display variables; falls back to a
    detached process with `systemctl --user show-environment` imported).
    Runs on XWayland (`QT_QPA_PLATFORM=xcb`) as a Qt.ToolTip +
    BypassWindowManagerHint + WindowTransparentForInput window, i.e.
    override-redirect and click-through, so it can sit above fullscreen
    windows; native Wayland fallback has only the stays-on-top hint.
    Settings: overlay.json `screen` (content full|simple, position, monitor,
    scale %, opacity %, margin), re-read every second.
  - *Viewers only*: drawing on the monitor but hiding it from the person
    is impossible (monitor capture = what the monitor shows). Offered
    instead: `puppetry-overlay share` (`overlay_cli.share()`): ensures the
    Browser Sources in OBS's current scene and opens a windowed projector
    of the program output (`OpenVideoMixProjector`), to share in Discord
    instead of the screen.
  - *Keybinds*: overlay.json `hotkeys` {screen, block, share} (key-name
    lists). `main.cpp` compiles each into a built-in macro
    (`__builtin_<key>`, native code `command("'<PUPPETRY_OVERLAY_CMD>'
    <subcommand>")`), so they use the normal combo matching. Part of
    `_restart_sig` (changing one restarts the daemon).
  - GUI: `gui/privacy_screen.py` `PrivacyScreenSection` on the Input
    Visualizer page (block status + button, ignored apps list with "+ Add
    app" from the open windows, on-screen overlay controls, share window,
    a `KeybindRow` per keybind using `ComboRecorder`).
  - Not verified on a real desktop: KWin stacking of the XWayland
    override-redirect window over fullscreen games, kdotool's
    `getwindowclassname` / `search` output on the installed version, the
    projector request against a real OBS.

**Not verified on real hardware / display.** Everything above ran under
`QT_QPA_PLATFORM=offscreen`; OBS pages were rendered in headless Chromium
(the engine behind OBS's browser source). Unchecked: a real OBS (tested
against a protocol-faithful fake server; the replay-output lookup by kind is
the least certain part), the daemon spawning the helper under systemd,
scan-code mapping on X11 vs Wayland (visualizer), drag feel in the block
canvas, evdev grab/repress behaviour, `kdotool` mouse position, `nix build`
of the flake/module, real controllers (axis ranges and trigger axes vary
by model; GAS/BRAKE are mapped to RT/LT) and games' reaction to the virtual
controller (it identifies as vendor 0x1234, not as an Xbox pad, so games
that only accept known pads may ignore it). Visualizer mouse movement is measured in-window
(pointer acceleration included); the overlay helper uses raw device counts.

**Planned, in rough priority.**
1. Next: real-desktop pass (OBS Browser Source + "Add to OBS" + replay
   length; the helper starting with the daemon; timing offset calibration
   against a real replay clip; `--list-devices` with a Steam Controller in
   desktop mode and in game); icon art (drop SVGs into
   `gui/ui_kit/resources/icons/`; names in `gui/ui_kit/icons.py`, including
   `nav_dictionary`); a real `nix build`. The afterglow side of the clip
   overlay (per-clip-type toggle + pipeline hook) is specified in the prep
   package and not built here.
2. Later: click a Dictionary entry to insert its block / preview it;
   clicking a key in the Dictionary map to edit its simplified names;
   renderer speed (render at 30 fps and duplicate, or skip unchanged
   regions); a numpad section for the keyboard; `Play Sound`;
   `--arguments=` CLI form; whole-app Ctrl+scroll zoom.
3. Someday: an "insert into macro" button on the visualizer readout;
   absolute-position mode for recorded mouse movement; per-key custom
   colors in the overlay.

**Open questions.** Whether the Dictionary should also preview the matching
block; which easing recorded movement should use (`linear` today); whether the OBS password should move from
`overlay.json` (mode 0600) to a keyring.

**Run the checks.** GUI: `cd gui && QT_QPA_PLATFORM=offscreen python3
test_app.py` (325 checks), `python3 test_overlay.py` (135 checks: layouts,
scene, movement-view math, replay file, helper over HTTP/SSE with a fake daemon, fake OBS
server, renderer + CLI, pieces/controller/source colors, transparency per
format, align/layer, JS parity; needs ffmpeg, node optional) and
`python3 ui_kit_test_kit.py`. Daemon: `cd native && mkdir build && cd build
&& cmake .. && cmake --build . -j && ctest -LE timing` (8 tests, incl.
`test_event_stream`). The sections below are the per-session history; the
newest facts are above.

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

(See "Start here" at the top for the current list; this one is kept only
for the items not yet re-homed there.)

- `puppetry --name="macro name" --arguments=(arg1, arg2, ...)` — an explicit
  named-flag syntax for CLI arguments, as an alternative/addition to the
  bare-trailing-args form already shipped. Needs a decision: replace or add.
- `Play Sound` — a macro-code function taking an absolute file path and
  playing it.
- DONE: `Wait for Macro Combo` (now `waitForReactivation`), `Listen for
  Button Press` (now `waitForPress`), block coding (Sessions 12-13), the
  Dictionary page (Session 16). Still open from the original Dictionary
  idea: a keyboard + mouse graphic where clicking a key edits its
  simplified name and aliases (the Input Visualizer's drawn keyboard,
  Session 16, is the reusable widget for it).
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
  - `gui/reference.py` — function/simplified-name reference text, plus
    the structured `PRIMITIVES` table (Session 12).
  - `gui/block_model.py`, `block_render.py`, `block_editor.py` — the
    block editor (Sessions 12-13): model + code round-trip, drawing, UI.
  - `gui/custom_blocks.py`, `custom_block_dialog.py` — the custom block
    library + its editor dialog (Session 13).
  - `gui/ui_kit/icons.py` — icon loader + the list of expected icon files.
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

## Session 11 — planning handoff: color scheme + icons, Dictionary page, block coding

**No feature code this session** — a writeup + decisions were requested
before switching to a new chat to actually build. One small fix was made
in passing (see below); everything else here is the plan for the next
three sessions, in the order requested: **(A) color scheme + icons
first, (B) Dictionary page second, (C) block coding last.** Do them in
that order — B and C both lean on data structures A/B introduce.

### Small fix made this session

`checkpoint()` (added in Session 10) was missing from `reference.py`'s
`DICTIONARY_TEXT` — the in-editor "Function reference" panel didn't
mention it at all. Added a normal entry for it, alphabetically where the
zero-arg primitives cluster. `gui/test_app.py` re-run clean (102/102)
after the change (it's plain text, no test exercises it directly, but the
run confirms nothing else broke). No native changes, no version bump.

### Decisions locked in (asked via AskUserQuestion this session)

1. **Block engine: a custom Qt canvas**, not embedded Blockly. Stays
   PySide6-native, matches the existing `ui_kit` widget/theme system, no
   new heavy dependency (no `pyside6-webengine`, no JS<->Qt bridge). This
   is the harder path (snapping/shapes built from scratch) but it's what
   he wants.
2. **Blocks and the text editor share one source of truth** — the macro's
   existing `code` string. Blocks compile down to it; existing/hand-typed
   code parses back up into blocks wherever it can, with an escape hatch
   (below) for whatever it can't. Both views stay switchable on the same
   macro, same as `simplified_names`/`python_on` toggles today.
3. **Icons: the owner is drawing them.** Don't invent placeholder
   icon *art* — build the *infrastructure* that expects icon files to
   show up later (a loader, a resources convention, a documented list of
   expected filenames/sizes) and degrade gracefully (text-only) wherever
   a file isn't there yet. This applies to A and, later, to per-category
   icons in the block palette (C).
4. **Color scheme: orange vs. blue, mapped to input vs. output** — his
   words: "try to have the choices be kinda clever, coloring things to
   match whether or not they are an 'input' or an 'output'." He may give
   exact hex codes later; the *concept* (input/output coloring) is
   staying regardless. He said "for now" on the specific hex values
   below — treat them as a reasonable starting proposal, not gospel.

### Phase A — color scheme + icon infrastructure (do this first)

**The input/output concept, applied across the whole app, not just
blocks:** Puppetry's entire domain already splits cleanly into "reading
the real world" and "acting on the real world" — lean into that instead
of inventing an arbitrary two-color scheme:

- **Orange = input** — anything that *reads* real hardware or *receives*
  a value: transcription (it records real keystrokes/mouse movement),
  the ping/hotkey/restart/checkpoint key pickers (`DetectKey`), the
  mouse-position readout, `arguments()` declarations, variable reads,
  conditions (in C, once ifs exist).
- **Blue = output** — anything that *synthesizes*/*acts*: macro
  playback in general, and specifically the primitives that inject
  events or act on the outside world — `kd`, `ku`, `tap`, `combo`,
  `type`, `move_mouse`, `wheel`, `command`. Variable *writes*/assignment
  arguably belongs here too (an assignment "outputs" a value into a
  name) — worth eyeballing once real blocks exist rather than deciding
  in the abstract.
- **A few primitives don't cleanly split** — `ignore`/`ignore_keys`
  block/modify future real input (arguably input-side, since they're
  about what's read), `actAs` remaps future input to different output
  (genuinely both), `checkpoint()` is a pure no-op marker (neither).
  Don't force these into orange/blue — give control-flow/neutral
  primitives and markers a third, neutral color (extend
  `ThemeSettings`, e.g. `color_neutral_block` alongside the accent/
  surface/highlight roles already there) rather than mis-coloring them
  for the sake of a clean binary.
- Proposed starting hex (dark-theme-appropriate, sits on the existing
  `#1e1e1e`/`#161616` backgrounds without fighting them): input orange
  `#e0955a`, output blue `#5a9ee0`, neutral `#8a8a8a` (close to the
  current `color_highlight`). These are a first pass to review
  on screen and adjust — don't treat them as final.
- Wire these in as new `ThemeSettings` fields (`color_input`,
  `color_output`, `color_neutral_block` or similar — follow the existing
  naming pattern in `theme_config.py`) with `Theme` accessors
  (`theme.input_color()` / `theme.output_color()`), so the *existing*
  live theme-editor page keeps working for these too — don't hardcode
  the hex anywhere outside `theme_config.py`'s defaults, exactly like
  every other color role in that file already works.

**Icon infrastructure (art comes later):**
- Add `gui/ui_kit/icons.py`: a small loader, e.g.
  `icon(name: str, color: QColor | None = None) -> QIcon | None`, that
  looks for `ui_kit/resources/icons/<name>.svg` (prefer SVG over the
  existing lone `checkmark_icon.png` — SVG lets an icon be recolored at
  runtime by find/replacing `fill=`/`stroke=` in the source before
  rendering to a `QPixmap`, which is how a monochrome icon can follow
  the theme's `text()`/`accent()`/input/output colors instead of being
  baked one color forever). Returns `None` (not a broken/blank icon)
  when the file doesn't exist yet, and every call site must handle that
  by falling back to text-only — icons will be dropped in
  incrementally, and nothing should look broken in the gap.
- Document the expected filenames up front (a comment block at the top
  of `icons.py` is enough) so it is clear what to draw and name: one per
  nav entry (`nav_macros`, `nav_editor`, `nav_visualizer`,
  `nav_settings`, and `nav_dictionary` once B lands), plus — once C
  lands — one per block category (`block_input`, `block_output`,
  `block_neutral`) and optionally per-primitive icons later
  (`primitive_kd`, `primitive_tap`, etc. — nice-to-have, not required
  for C to function).
- Wire nav buttons in `app.py` to show an icon beside the label when one
  resolves, text-only otherwise. This is the only UI change this phase
  actually needs to make visible — the rest is plumbing for B and C.

### Phase B — Dictionary page (do this second)

Currently the "reference" is two `Collapsible` text blobs
(`DICTIONARY_TEXT` and `simplified_names_reference_text()`) stuffed into
the macro editor's left column (`editor_page.py`, "references + aliases"
section). Replace this with a **dedicated nav page** (new `PAGE_DICTIONARY`
in `app.py`, using Phase A's icon slot).

**The real work here isn't the page, it's turning `DICTIONARY_TEXT` from
a prose blob into structured data**, because Phase C's block palette needs
exactly the same information (name, parameters, category, description) and
must not re-derive it independently. Concretely:

- Replace the `DICTIONARY_TEXT` string in `reference.py` with something
  like a list of small dataclasses/dicts — one per primitive — each
  carrying: `name` (`"tap"`), `params` (structured enough to render a
  signature AND, later, to generate block input sockets — e.g.
  `[("key", None), ("time_", "0.1")]` for required vs. keyword-with-default),
  `category` (`"input" | "output" | "neutral"`, per Phase A's scheme —
  this is the categorization decision from Phase A, applied concretely
  per-primitive here), and `description` (the existing prose, unchanged).
  Keep a plain-text render function for anywhere that still wants the old
  blob format (there may not be anywhere by the time this is done, but
  don't break `simplified_names_reference_text()`'s callers without
  checking first).
- The Dictionary page: searchable (a line edit filtering by name/alias as-
  you-type is enough, doesn't need to be fancy), grouped by category with
  each entry's signature colored by Phase A's input/output/neutral colors,
  and each primitive's simplified-name aliases (currently
  `simplified_names_reference_text()`) shown inline with its entry instead
  of as a separate wall of text — much more useful than two disconnected
  panels.
- Once the page exists, remove the two `Collapsible` panels from
  `editor_page.py` and replace them with a single line/button pointing at
  the new Dictionary page (`w.nav.button(PAGE_DICTIONARY).click()`-style
  navigation, matching how `open_editor()` etc. already jump between
  pages in `app.py`).
- Add `gui/test_app.py` coverage: the structured primitive table has one
  entry per real primitive (cross-check against the native side's actual
  registered names so the dictionary can't silently drift out of sync —
  worth a small assertion comparing `reference.py`'s primitive list against
  the names in `native/src/native_vm.cpp`'s `compile_op()`/
  `python_embed.cpp`'s `FC(...)` table, even if that means listing them by
  hand in the test and failing loudly if native adds one this file doesn't
  know about), search filtering, and page navigation.

### Phase C — block coding (do this last)

The biggest piece, and it depends on B's structured primitive table
existing first.

**Scope, from the request:** "basic python functions (loops, variable
creation, etc)" + "all of the built in functions" + puzzle-piece-style
snapping with visual top/bottom indicators. Concretely that means block
shapes for:
- Every registered primitive from Phase B's table (`kd`, `ku`, `tap`,
  `combo`, `type`, `move_mouse`, `wheel`, `wait`, `speed`, `checkpoint`,
  `ignore`, `ignore_keys`, `actAs`, `command`), each colored per its
  Phase A/B category.
- `arguments(...)` as a special top-of-macro-only block (mirrors its
  "FIRST LINE ONLY" text rule today).
- Cross-macro calls by name (already resolved via the registry at
  compile time — a block that lets you pick another macro from a
  dropdown, same list `alias_targets()`/the macro list already builds
  elsewhere).
- Loops (`for _ in range(n):`, and probably `while <cond>:` — the request said
  "loops" generally; confirm which shapes he actually wants once you're
  building rather than guessing both are required).
- Variable creation/assignment (`x = <value>`) and variable-read blocks
  usable as an input socket anywhere a value is expected.
- **Not explicitly requested but implied by "basic python functions"**:
  `if`/`elif`/`else` conditionals. Flag this rather than silently
  building or silently skipping it — "loops, variable creation, etc" could
  mean he wants conditionals too, or could mean he's deliberately starting
  narrower.

**Compile-time constraint that shapes the whole design:** loops,
variables and conditionals are Python control flow, and the native fast
path (`python_on=False`) categorically rejects control flow at compile
time — `test_native_vm.cpp` has a standing regression test proving
`if True:` is a compile error there (see Session 3/4's native rewrite).
So: a macro built with any loop/variable/if block **must** be
`python_on=True`; a macro built from primitives-only blocks (no
loop/var/if) can stay in whichever mode the macro's existing toggle says.
The block editor should probably auto-flip `python_on` on (with a visible
note, not silently) the moment a loop/variable/if block is dropped in,
and let the user flip it back only after removing them.

**Round-trip (per decision #2 above) — this is the hard part:**
- **Blocks → code:** straightforward top-down codegen, block sequence to
  indented Python lines. The puzzle-piece "snap" relationships directly
  encode nesting (a block dropped into a loop's "mouth" becomes an
  indented child) — build the block data model around a tree, not a flat
  list, from the start.
- **Code → blocks:** parse with Python's own `ast` module (macros are
  already required to be valid Python when `python_on=True`, so `ast.parse`
  is the right tool, not a hand-rolled parser) and walk the tree building
  the matching block for each node type it recognizes.
- **The escape hatch, and why it's necessary:** hand-typed code will
  always be able to express things no block exists for yet (arbitrary
  expressions, imports if anyone ever writes one, weird formatting).
  Round-tripping "blocks are just another view of the same code" (decision
  #2) only holds up if nothing is ever silently dropped or corrupted going
  code → blocks → code. Give every unrecognized statement/line a generic
  "raw code" block — still snaps top/bottom like any other piece, shows
  the literal text, isn't editable via sub-blocks — so switching to Block
  view and back never loses or mangles anything it doesn't fully
  understand. This is more important to get right than any individual
  block shape.
- Native-path (`python_off`) macros are simpler in this direction: they're
  already restricted to one primitive/macro-call per line with literal
  args, so parsing them into blocks doesn't need `ast` at all — the
  existing line-oriented grammar the native compiler already uses
  (`native_vm.cpp`'s statement parsing) is the reference for what a line
  can look like; reuse its shape rather than inventing a second grammar
  for the same restricted language in Python.

**Visual/interaction notes:**
- Puzzle-piece silhouette (not just a Scratch-style flat-topped tab) per
  the requested wording — actual notch/tab geometry on a `QPainterPath`, not a
  plain rectangle, so blocks visually interlock rather than just abutting.
- Snap feedback: highlight the target notch (or the whole receiving edge)
  when a dragged block gets close enough to snap, distinct from the
  block's own category color so it reads as "about to connect" rather
  than changing the block's identity color.
- `QGraphicsView`/`QGraphicsScene` is the natural fit for a draggable,
  zoomable canvas in Qt — each block a `QGraphicsItem` subclass with its
  own `paint()` drawing the puzzle silhouette in its category color, and
  scene-level logic doing hit-testing against nearby blocks' notches to
  decide when a drag-release becomes a snap.
- A visible toggle on the macro editor page to switch between "Blocks"
  and "Text" for the *same* macro (not a separate page — same spot the
  code editor lives today), matching decision #2.

**Testing:** this is the piece most likely to have subtle bugs (codegen
correctness, round-trip fidelity, snap hit-testing), so budget real time
for `gui/test_app.py` coverage of: blocks → code for each block type,
code → blocks → code round-trip (build code by hand, parse to blocks,
regenerate, assert it matches — byte-for-byte where reasonable, or
semantically equivalent where whitespace/formatting can't be preserved
exactly), the raw-code escape hatch actually firing on something
unrecognized instead of crashing or eating it, and the python_on
auto-flip behavior.

### Suggested next-session opening move

Don't try to do all three phases in one sitting — they're sized like
Session 3 (the whole Qt rewrite), not like Session 9/10. Start a fresh
chat per phase (or at least checkpoint/re-zip between A and B, and
definitely between B and C — C is the large one). Read this section plus
the "Architecture / file map" section below before touching code.

## Session 12 — block coding (Phase C, done first by choice)

Block coding was chosen as the **first** piece rather than Phase A/B, and added
one requirement on top of the Session 11 plan: **which view macros open
in (Blocks or Text) is a global setting, and any macro can be flipped to
the other view at any time.** Everything below is built; A and B are still
open, but the two pieces of them C needed were built minimally here (see
"What C pulled forward from A/B").

Verified: `gui/test_app.py` 102 -> **161** (+59), `gui/ui_kit_test_kit.py`
still green, native untouched (built it only so the GUI tests could
validate generated code with the real `puppetry-daemon --check`).
**Not verified on a real display** — every screenshot was offscreen. Do a
real drag-and-drop pass before trusting the feel (snap distance, zoom,
drag autoscroll, menus).

### What is visible

- Macro editor, above the code: a **Blocks | Text** segment toggle (where
  the "Macro code" title was). Same code underneath; switch whenever.
- **Settings > Behavior > "Macro editor opens in" [Blocks | Text]** — the
  global default (`state.json` `editor_default_mode`, default `"blocks"`).
  Every macro opens in that view; the per-macro choice is NOT remembered
  between opens (deliberate: the default stays predictable. If wanted
  per-macro memory, store `{macro_id: mode}` via `model.set_pref` in
  `set_view_mode()` — don't put it in the macro dict, that would dirty the
  macro just from switching views).
- Block view = palette on the left (Output / Timing & control / Real
  input / Variables / Other macros / Other), canvas on the right with an
  input-orange **hat** ("when KEY_HOME pressed" — the macro's combo, or
  "when this macro runs"). Only the stack under the hat is the macro.
- Interactions: drag a block = it and everything below it (Ctrl-drag =
  just that block); a dashed ghost shows the snap; drop on the palette =
  delete; click a socket = inline editor (dropdown for True/False,
  easing, ignore target, `+= -= *= /=`, macro names; key sockets get a
  completer from `--dump-names`); `⋯` on a block shows/hides its optional
  settings; right-click = duplicate / delete / delete-below / add else-if /
  add-remove else / unwrap / show all options / edit as text / turn raw
  text into blocks; drag empty space = pan; Ctrl+wheel = zoom; Ctrl+0;
  Ctrl+Z / Ctrl+Shift+Z (or Ctrl+Y); Delete = delete selected; hover =
  primitive summary tooltip; "Tidy up" lines up loose stacks.
- Variables section: `arguments(...)`, `set [x] to`, `change [x] +=`,
  one draggable orange chip per variable the macro defines (arguments
  params, assignments, loop vars) + "+ New variable". Drop a chip on any
  socket to use it. Sockets holding a known variable draw orange;
  conditions draw as orange hexagons (input = orange, per Phase A).
- Loose blocks (not under the hat) draw faded, the toolbar says they
  won't be saved, and Save's status line repeats it.

### Decisions made while building (flag any that are disputed)

- **Conditionals were built** (if / else if / else), plus `while`,
  `repeat N times` (`for _ in range(N)`) and `for each [i] in [iter]`.
  Session 11 said to flag if/elif/else rather than guess — it's built and
  flagged here; removing it is just dropping two palette entries.
- **Sockets hold source text, not nested reporter blocks.** A variable
  "reporter" is dragged onto a socket and fills it with the name (drawn
  orange). No Scratch-style nested expression blocks (`x + 1` is typed).
  This keeps every socket lossless and was far simpler; nested operator
  blocks could be layered on later without changing the model.
- **python_on auto-flip:** dropping a loop/variable/if block into a
  `python_on=False` macro turns "Run as embedded Python" on with a visible
  note; unticking it is refused (with a note) while such blocks exist in
  the macro. Text view keeps its old behavior (the checker catches it).
- **Colors:** control flow = neutral grey (slightly darker), timing/
  markers/ignore/actAs = neutral, injectors = output blue, `arguments`/
  hat/variable reads/conditions = input orange, `set`/`change` and
  macro-calls = output blue (a write "outputs" a value). Comments/raw
  text = darker neutral. Socket fill = `theme.text()` (no hex hardcoded
  outside `theme_config.py`).

### Round-trip fidelity (the part to not break)

`gui/block_model.py` is Qt-free and holds it all:
- `code_to_blocks()` uses `ast` (for python_off code too — its one-call-
  per-line grammar is valid Python, so no second parser was needed).
  Anything with no block (imports, try, with, def, `a; b`, one-line
  `if x: tap(A)`, starred args, unknown calls...) becomes a **raw block**
  holding its exact source lines — never dropped. Comments become comment
  blocks (or stay inside raw blocks); blank lines are `blank_before`;
  `# trailing` comments are kept per block/header; indentation unit
  (tabs / 2 spaces / 4) is detected and reused for new blocks.
- Every parsed block keeps its original text (`Block.src`) and re-emits
  it verbatim until its sockets are edited (`touch()`), so odd spacing
  like `tap( KEY_A ,0.2)` survives even after the block is moved.
- **The editor never regenerates code at all unless the blocks were
  edited** (`editor_page.code_text()` compares `BlockEditor.revision` to
  the last sync). So opening a macro in Blocks and switching back is a
  guaranteed no-op, whatever the parser thinks.
- Parser note: `ast.get_source_segment()` re-splits the whole file on
  every call — quadratic; a 2k-line transcription took 19 s. `_Parser._seg`
  slices cached lines instead (20k lines: parse 0.4 s, first paint 0.4 s,
  a socket edit ~0.7 s incl. undo snapshot + relayout). Undo snapshots use
  `Block.copy_tree()`, not `deepcopy` (2.5x faster).
- Syntax errors: the macro opens / stays in Text with "can't be shown as
  blocks until its syntax error is fixed (line N: ...)".

### Transcription while in Block view

Transcription still writes text: in Block view it always appends at the
end (no visible text cursor), and the blocks are re-read from the text
250 ms after each batch (and once more on stop). Clear-before-transcribing
/ checkpoint logic is unchanged (it runs on the synced text first). A
re-read drops loose stacks and undo history — acceptable mid-recording.

### What C pulled forward from A/B

- **Phase A (partial):** `ThemeSettings.color_input` `#e0955a`,
  `color_output` `#5a9ee0`, `color_neutral_block` `#8a8a8a` + `Theme.
  input_color()/output_color()/neutral_block_color()/category_color()`;
  they appear in the existing theme editor automatically. **Not done:**
  applying orange/blue to the rest of the app, `icons.py`, nav icons.
- **Phase B (partial):** `reference.py` now has the structured
  `PRIMITIVES` table (`Primitive`/`Param` dataclasses: name, params with
  defaults/kind/caption/choices/kw_only/vararg, category, summary). The
  block palette, block labels, sockets and parser all read it. A test
  asserts its names equal the `FC("...")` table in `python_embed.cpp`
  exactly. **Not done:** the Dictionary page; `DICTIONARY_TEXT` is still
  the prose blob and the two Collapsibles are still in the editor. Note:
  `DICTIONARY_TEXT` says `ignore(target)` but the daemon's keyword is
  `what` — the table uses `what`; fix the prose when B happens.

### Files

- `gui/block_model.py` — Block/Stack/Doc, `code_to_blocks`,
  `blocks_to_code`, factories, `needs_python`, `variable_names`.
- `gui/block_render.py` — geometry constants, puzzle silhouettes
  (`stmt_path`, `c_path`, `hat_path`; notch cut into every top edge, tab
  under every bottom edge, mouths get both), layout (numbers only; paths
  built lazily on first paint/hit; `StackLayout.visible()` bisects so a
  20k-block stack paints only what's on screen), painting.
- `gui/block_editor.py` — `BlockEditor` (palette + toolbar + canvas; undo;
  all mutations), `BlockView` (mouse/drag/zoom/drops), `StackItem` (one
  per stack; the main one owns the hat), `GhostItem` (snap preview, z
  between stacks and the dragged stack), `Palette`/`PaletteBlock`/
  `VariableChip` (QDrag with `application/x-puppetry-block` /
  `-variable` mime).
- `gui/editor_page.py` — the toggle, `code_text()`, `set_view_mode()`,
  python auto-flip, transcription hooks, `sanitize_macro_name()` (mirror of
  `native/src/macro.cpp`'s), `self.left_scroll` (test hook).
- `gui/settings_page.py` — the default-view combo.
- `gui/test_app.py` — `block_tests()`: byte-for-byte round-trip over 19
  samples (comments, blanks, elif/else, tabs, 2-space indent, unicode,
  multi-line strings, raw constructs), raw escape hatch, codegen for every
  block type, every palette primitive compiling on the **native** path via
  the real daemon, control-flow blocks compiling as Python, primitive
  table vs. native, real mouse drag-away / drag-back-with-ghost / Ctrl-
  drag / undo / redo, inline socket editing, variable drop, palette drop,
  python_on lock + auto-flip, arguments-only-at-top snapping, duplicate /
  delete / edit-as-text / turn-into-blocks, Text<->Blocks after edits,
  syntax-error fallback, transcription in Block view, loose blocks not
  saved, and the global default (both values).

### Known gaps / ideas

- Blocks don't slide apart to make room while hovering (Scratch does);
  the ghost marks the spot instead.
- No per-socket key *detector* ("press the key you mean") — typing with a
  completer only. `DetectKey` already exists and would slot into
  `BlockEditor.edit_field()` for `kind == "key"` sockets.
- Very long raw/comment lines aren't wrapped; the block just gets wide.
- Palette is a fixed 300 px column; on a 1400 px window the canvas is
  narrow unless the settings splitter is dragged left.

## Session 13 — native interpreter, new primitives, block editor round 2, app-wide colors

The list after trying Session 12, plus two mid-session additions. All
built. Verified: native ctest **8/8** incl. the new `test_native_interp`
(67 checks, differential native-vs-Python), `gui/test_app.py` 161 ->
**215**, `ui_kit_test_kit.py` green. Still **offscreen only** -- do a real
display + real hardware pass (see "Needs a real-machine check" below).

### 1. The native path is now a real interpreter (chosen)

`native/src/native_vm.cpp` was rewritten: tokenizer (Python INDENT/DEDENT,
triple-quoted/raw strings, line continuations) -> recursive-descent parser
-> AST -> compile pass that resolves every name once (locals to slots,
key names to constants) -> closure tree. Supports: variables, `+ - * / //
% **`, comparisons (chained too), `and/or/not`, `x if c else y`,
if/elif/else, while, for-in (`range()` is lazy -- `range(10**10)` is
fine), break/continue, `def`/`return` (per-macro functions; they can read
macro-level variables, `nonlocal` to write them; recursion capped at 200),
top-level `return`, lists/tuples, indexing, tuple unpacking (`x, y = ...`,
`for a, b in ...`), builtins `int float str bool len abs round min max
range list print`, every primitive, other macros/custom blocks by name.
Rejected AT COMPILE TIME with a clear message (so Save catches it): import,
class, try, with, lambda, attribute access other than `.x/.y`, method
calls, f-strings, slicing, dicts, nested def. Runtime errors carry the
line number. Every loop iteration checks the abort flag.
- `tests/test_native_interp.cpp` runs ~30 programs through BOTH backends
  and requires identical output (a probe "macro" records values), plus
  compile-error, abort, lazy-range and wait-primitive checks. Add a case
  there whenever the interpreter grows.
- Semantics match Python except: defs are hoisted (a function can be
  called above its `def` natively; Python would NameError) and primitive
  ARGUMENTS stay lenient (`tap("30")` works) like before.
- Hot path: a literal-args primitive line is still one closure over
  pre-resolved constants; typed `Arg` accessors avoid Value copies.
  bench_cps: ~2.2-2.4M taps/s vs ~2.5M before (within noise-ish; the extra
  cost is one more std::function hop per statement).
- The block editor no longer forces "Run as embedded Python" on for loops/
  if/variables (Session 12's auto-flip is gone) -- native runs them.
- Python-path fix found by the differential test: multi-line triple-quoted
  strings used to get the function-body indentation added to their
  CONTENT (`type("""a\nb""")` typed "a\n    b"). `python_embed.cpp`'s
  indent now skips lines that start inside a string.

### 2. New primitives (both paths, `primitives.cpp` + `python_embed.cpp` + native)

- `getMousePosition()` -> a position: a real `(x, y)` tuple with `.x`/`.y`
  (Python: `MousePosition(tuple)` subclass; native: a tuple Value with
  `is_point`). `getMousePosition.x` / `.y` read it live without the call
  (Python: the global is a callable object with properties). Uses the
  cursor cache when fresh, else kdotool; errors clearly without kdotool.
- `move_mouse(pos)` takes a saved position; a real saved position defaults
  to `move_to=True` (absolute) unless move_to is passed. A plain tuple
  stays relative unless move_to=True.
- `getButtonsHeld()` -> list of REAL held key codes (`KEY_A in
  getButtonsHeld()` works on both paths).
- `waitForPress(button="any", repress=False)` -> blocks until that real
  key/button is pressed and RETURNS its code; blank / None / "any" / "all"
  = any key or mouse button (`b = waitForPress()` then `tap(b)`).
  repress=True swallows that press (see mechanics below).
- `waitForReactivation(repress=False)` -> blocks until THIS macro is
  triggered again (combo or `puppetry --name`); that trigger is consumed
  (doesn't start/toggle/stop anything). repress=True swallows the combo
  keys' presses while waiting. Uses a thread-local "current macro" set by
  macro threads (`current_macro()` in macro.cpp).
- Mechanics: `Runtime::repress_codes` (counted) / `repress_any` make the
  grab logic grab the device and `should_forward` drop those presses;
  `watch_device` now decides forwarding BEFORE handling the event, so a
  waiter that wakes and drops its repress codes can't leak the press it
  waited for. Waiters poll every 1 ms and are abortable; cleanup is RAII.
- Macro arguments passed as strings are now converted to the type of that
  parameter's default (`coerce_macro_arg`): `arguments(hits=3)` gets 5
  from "5", `arguments(key=KEY_A)` gets KEY_B's code from "KEY_B", bools
  from "True"/"False". Both paths. (Needed for custom blocks + "run macro
  with arguments", and just nicer.)

### 3. Custom blocks (daemon side)

`~/.config/macro-daemon/custom_blocks.json` (`gui/custom_blocks.py`). The
daemon (`main.cpp`) registers each entry's `code` under
`sanitize(name)` like a macro with no combo, and adds the names to the
Python trampoline list, so any macro can call `click_at(100, 200)` on
either path. The GUI generates `code` = `arguments(<arg>=<default>, ...)`
+ the template with `{arg}` -> `arg`, and validates it with `--check`
before saving. Saving/deleting a custom block restarts the daemon.

### 4. Block editor changes (the full list)

- **Growing list sockets**: every varargs socket (combo keys, ignore keys,
  act as, run command's values, arguments, run macro/function arguments,
  function parameters) shows its values + one empty `+` slot; filling it
  adds another, clearing one removes it. Model: those fields are Python
  lists (`bm.set_socket` handles append/remove).
- **Plain-English labels** from `Primitive.label` (`actAs` -> "act as",
  `move_mouse` -> "move mouse", `kd`/`ku` -> "key down"/"key up",
  `wheel` -> "scroll", `command` -> "run command", ...). A test enforces no
  camelCase/snake_case in labels.
- **Reporters** (`block_model.Rep`, nestable, drawn inside sockets):
  variables, true/false (**green / red**, click to flip), "[] is [equal to
  ▾] []" (six ops, hexagon), "[key] is held", mouse position / mouse x /
  mouse y, buttons held, "key pressed" (`waitForPress()` as a value). The
  parser turns matching code into these (`x >= 5`, `KEY_A in
  getButtonsHeld()`, `getMousePosition.y`, `b = waitForPress()`...).
  **Drag any reporter OUT of its socket** into another one; drop it on
  nothing (or the palette) to remove it.
- **Palette**: Output (+ "run macro"), Timing & control (+ true, false),
  **Conditions** (if, if/else, compare, is held), Real input (wait for
  press / reactivation, ignore, ignore keys, act as, key pressed, mouse
  position, buttons held), Variables, **Functions**, **My blocks**, Other
  (note, custom code). Wide blocks shrink to fit.
- **Run macro**: one block, "run macro [name ▾] with arguments [..][+]".
- **Notes anywhere**: a note dropped away from a stack (or dragged off one
  on its own) becomes a free-floating card with no notch/tab. Saved as a
  `#@note x,y: text` comment line at the end of the code (the daemon
  ignores comments), so position and text survive. Drag it into a stack to
  attach it again.
- **Custom code block** = the old raw block, now purple and named so.
- **Create a custom block**: "My blocks > + Create a custom block" opens
  `custom_block_dialog.py`: name, category (any palette section), color
  (orange/blue/gray/purple presets or any hex), arguments (add/remove;
  default; choices from any value / keyboard keys / mouse buttons /
  keyboard + mouse / a custom list -> dropdown or completions), code
  template, embedded-Python toggle, tooltip. Right-click a custom block in
  the palette to edit/delete it.
- **Per-macro functions**: "create function [name] with [params][+]" hat;
  blocks snap under it; "run function [name ▾] with arguments", and
  "return [value]". Code: top-level `def`s, emitted after `arguments()` and
  before the main script. Functions are their own stacks (auto-placed to
  the right; positions aren't saved -- only notes' are).
- **Hat moves** the whole script (was already true; now tested), and a
  function's hat moves the function (drop it on the palette to delete).
- **Tooltips** on every block, socket (per-param docs in `Param.doc`),
  reporter and palette entry.
- **Undo transcriptions**: Block view pushes one undo snapshot when a
  transcription starts and re-reads keep that undo stack; Text view does
  the clear + every inserted chunk inside ONE QTextDocument edit block
  (it used to `setPlainText`, which wiped undo history).
- **Scroll bars only when needed**: the scene rect is the blocks' bounds +
  a small margin, at least the visible area.

### 5. App-wide color scheme + icons

- Orange (input): record combo, every key picker (ping/transcribe/restart/
  checkpoint/abort), device Detect buttons, Start Transcribing, the mouse
  readout, "Transcribe Inputs"/"While this macro runs" titles, macro-row
  combo buttons, the Input Visualizer's title and counters. Blue (output):
  sound previews, the new Settings "Playback (virtual keyboard + mouse)"
  group (pointer accel + real-time priority moved there). Helpers:
  `widgets.mark_input()` / `mark_output()`. New theme roles (editable in
  Settings > Appearance): `color_custom` (purple), `color_function`
  (pink), `color_true` (green), `color_false` (red).
- **Icons: waiting on the art.** `gui/ui_kit/icons.py` documents the
  exact filenames (nav_*, block_* per palette section, optional
  primitive_<name>) and loads `resources/icons/<name>.svg` (recolored via
  `currentColor`/black -> the theme color where it's shown) or `.png`.
  Wired into the sidebar (icon left of the label) and the palette (section
  headers, per-primitive). Missing files = text only; nothing breaks.

### Needs a real-machine check

- waitForPress/waitForReactivation with repress=True on real devices (the
  grab + forwarding path is exercised in tests only via notify_press and
  the repress sets, not a real evdev grab).
- getMousePosition via kdotool on KDE; move_mouse(pos) accuracy.
- Drag feel: reporter drag-out threshold, note dragging, hat dragging,
  palette shrink-to-fit on a real DPI.
- Custom block Save restarts the daemon (same `systemctl --user restart`
  as macro Save).

## Session 14 — no-scroll dropdowns, batched autosave, macro categories, colors everywhere

Verified: `gui/test_app.py` 215 -> **239**, `ui_kit_test_kit.py` green
(its purple expectations updated), native ctest unchanged/green. Offscreen
only, as always.

- **Mouse wheel never changes a dropdown, spin box or slider** anywhere in
  the app. `widgets.WheelGuard` (installed app-wide by `MainWindow`) eats
  wheel events on QComboBox / QAbstractSpinBox / QAbstractSlider (not
  scroll bars) and forwards them to the nearest scroll area, so the page
  scrolls instead. An OPEN dropdown list still scrolls normally.
- **Batched autosave** (`AppModel.request_save`, used by autosave's
  `mark_dirty`): the first save after a quiet second is immediate; a save
  requested within a second of the last one starts a 1 s timer that every
  further request restarts; when it fires, ONE save writes everything and
  restarts the daemon once. Explicit Save buttons still save immediately
  (and cancel a pending batch). Quitting flushes a pending batch.
- **Macro categories** (separate from profiles): macros.json gets
  `"categories": [{"name", "enabled"}]` (ordered) and each macro an
  optional `"category"`. The daemon (`main.cpp`) turns a macro off when its
  category is off, whatever the profile says; each macro's own switch is
  kept. Macros page: one header per category (collapse arrow -- collapsed
  state is a state.json pref -- click-to-rename name, count, on/off switch,
  move up/down, delete = macros move to Uncategorized); rows in a
  switched-off category are dimmed. "+ New Category" at the bottom;
  right-click a row -> "Move to category" (or "New category…"); the
  editor has an editable Category dropdown. With no categories the page
  looks exactly like before (no headers).
- **Profile switch from the Macros page**: "Profile: X ▾" top-left is a
  button with a menu of profiles (same unsaved-changes prompt as Settings;
  shared via `widgets.switch_profile_interactive`).
- **Colors, broader**: new theme role `color_general` (purple) = anything
  that's neither input nor output: default button fill
  (`Theme.button_color()`), sidebar + Blocks/Text segment buttons. Green
  (`color_true` = enabled) for ON switches and ticked checkboxes; red
  (`color_false` = disabled) for OFF switches, Delete/Remove/✕ clear
  buttons (`widgets.mark_disable`), a locked macro's lock, and
  Delete/Discard/Quit/Remove in confirm dialogs. Orange/blue as before.
  (Checkbox/switch/button painting changed in the ui_kit copy -- noted
  inline as "Puppetry:" so a kit sync doesn't silently undo it.)

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


## Session 15 — palette clipping + symmetric scroll bars

- Palette reporters (`PaletteReporter`) shrink to fit and are packed into rows by measured width
  (`Palette.USABLE_W`, wraps at the palette edge) instead of a fixed 2-per-row -- "mouse position" no longer
  runs under the scroll bar. Palette margins are 6/6.
- `CustomScrollBar` insets its track and handle by `INSET` (3px) on all sides so the gap is equal either side
  of a vertical bar / above and below a horizontal one; `SmoothScrollArea` now installs the custom bar on the
  horizontal axis too, so in-panel bars match.

## Session 16 — Dictionary page + Input Visualizer keyboard/mouse

- New: `gui/dictionary_page.py`, `reference.parse_dictionary()`,
  `LANGUAGE_ENTRIES`, `signature_of()`, `key_alias_rows()`; `PAGE_DICTIONARY`
  in `gui/app.py`; editor button replaces the two collapsibles.
- New: `gui/input_transcript.py` (event log -> macro text), keyboard/mouse
  drawing and event capture in `gui/visualizer_page.py` (`CpsArea`,
  `KEY_ROWS`, `NAV_KEYS`, `key_name_of`).
- Tests: `gui/test_app.py` 240 -> 265 checks (dictionary coverage against
  the primitive table, search, empty state, editor button; transcript
  rendering rules; live readout from simulated key/mouse/wheel events).
- README cleanup: personal names removed, duplicated "Next" list collapsed,
  "Start here" section added.

## Session 17 — OBS overlay, layered replay buffer, hold timers, curved arrow, Dictionary rework

- Daemon: `native/src/event_stream.{hpp,cpp}` (Unix-socket broadcast of
  real + macro input), hooks in `dispatch.cpp` (per-frame motion/wheel sums)
  and `uinput_device.cpp`, `runtime_dir()` / `event_socket_path()` /
  `overlay_config_file()` / `load_overlay_config()` in `config.*`, helper
  spawn/respawn in `main.cpp`, `--dump-names` now includes `codes`
  (name -> code). New test `tests/test_event_stream.cpp`.
- GUI, new files: `kbm_layout.py`, `kbm_paint.py`, `overlay_config.py`,
  `overlay_server.py`, `overlay_render.py`, `overlay_cli.py` (the
  `puppetry-overlay` entry point), `obs_client.py`, `overlay_settings.py`,
  `overlay_web/{common.js,kbm.js,full.html,simple.html,mouse.html}`,
  `test_overlay.py`.
- GUI, changed: `visualizer_page.py` (shared painter, timers, arrow,
  overlay section), `dictionary_page.py` (3x text, collapsible entries,
  key-name map), `reference.py` (descriptions reflowed into paragraphs),
  `app.py` (flush overlay settings on close).
- Nix: `puppetry-overlay` wrapper (ffmpeg on its PATH) in both `flake.nix`
  and `module.nix`; the service gets `PUPPETRY_OVERLAY_CMD`.
- Bugs found while testing: a status-file write race between the helper's
  threads (fixed with per-thread temp names + a lock);
  `HTTPServer.shutdown()` hanging while a Server-Sent Events handler was
  streaming (replaced `serve_forever` with a `handle_request` loop that stops
  on a flag); a float rounding that showed 1.099 for 1.100 s in timers.

## Session 18 — controller support, separate pieces, input/output colors, afterglow sidecar design

- Daemon: `evdev_device.*` (`device_has_abs`, `find_best_controller`,
  `normalize_abs_range`, per-device abs ranges), `uinput_device.*`
  (`create_gamepad`, `abs_frame`), `primitives.*` (`axis_code`, `axis_fn`,
  `get_axis_fn`, gamepad routing, abort recenters), `dispatch.cpp`
  (controller EV_ABS, never grabbed), `main.cpp` (controller thread with
  hotplug, virtual controller, overlay helper also for `controller`),
  `python_embed.cpp` / `native_vm.cpp` (`axis`, `getAxis`),
  `event_stream.*` (`a` lines, axes in the hello). Tests: `test_core`
  (normalization), `test_native_interp` (getAxis parity, axis() needing the
  virtual controller, output frames).
- GUI: `kbm_layout.py` (controller layout, pieces, sources, axes),
  `kbm_paint.py` (controller drawing, source colors), `overlay_web/*`
  (same in JS; `mouse.html` -> `movement.html`), `overlay_config.py`
  (split/controller/movement, migration), `overlay_server.py` (pages,
  axes, macro motion), `overlay_render.py` (all modes, colored simple
  parts, `align`, `layer`), `overlay_cli.py`, `overlay_settings.py`,
  `visualizer_page.py` (`StreamClient`), `settings_page.py`,
  `input_tools.py`, `reference.py`, `block_model.py`, `block_editor.py`,
  `block_render.py`.
