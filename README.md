# Puppetry

A keyboard/mouse macro daemon for NixOS (C++, with embedded Python for macros), with a Qt editor. Watches your
real keyboard/mouse read-only (never grabs them) and fires macros through a
virtual `uinput` device when you hit a configured combo. Works under both
KDE Plasma (KWin) and Hyprland on Wayland.

Macros are just Python: `tap()`, `kd()`/`ku()` (key down/up), `move_mouse()`
(relative or absolute -- `move_to=True` moves the cursor to an exact screen
position instead of by an offset), click helpers, hold/toggle/repeat modes,
and macros can call each other by name.

## Requirements

- NixOS with flakes enabled
- **KDE Plasma (KWin)** for `move_mouse(..., move_to=True)` and the live
  "Mouse position" readout in the editor -- both need `kdotool`, which reads
  the cursor position via KWin's scripting D-Bus interface. Under Hyprland
  those two features fall back to a corner-anchored move every time (still
  correct, just always via the top-left corner instead of the shortest path).
  Everything else works fine on either compositor.

## Installing

Add this repo to your flake's inputs:

```nix
{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    puppetry.url = "github:YOUR_GITHUB_USERNAME/puppetry";
    # Optional but recommended -- makes puppetry reuse YOUR pinned
    # nixpkgs instead of fetching its own copy:
    puppetry.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, puppetry, ... }: {
    nixosConfigurations.yourhostname = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        puppetry.nixosModules.default
        {
          services.puppetry = {
            enable = true;
            user = "yourusername";  # whoever should get input-device access
          };
        }
      ];
    };
  };
}
```

Then:

```fish
sudo nixos-rebuild switch --flake .
```

(swap in whatever your usual rebuild command is -- `nixos-rebuild-flaked`,
plain `nixos-rebuild switch`, etc.)

**Log out and back in** afterward -- group membership needs a fresh login
session to take effect.

## Using it

- The daemon starts automatically on login as a per-user systemd service.
- Launch the editor by running `puppetry`, or find **Puppetry** in your
  app launcher / search menu (KRunner, wofi, rofi, etc.).
- Check it's alive: `systemctl --user status macro-daemon`
- Watch logs live: `journalctl --user -u macro-daemon -f`
- Config lives at `~/.config/macro-daemon/` (`state.json` + `profiles/*.json`)
  -- created automatically on first run.
- Hitting **Save** in the editor restarts the service for you automatically.
- Macros can be edited as snap-together **Blocks** or as **Text** -- the
  toggle above the code flips any macro between them (same code
  underneath). Which one macros open in is under **Settings > Behavior**.
- Make your own blocks under **My blocks > Create a custom block** -- they
  work in every macro.
- Icons: drop SVGs into `gui/ui_kit/resources/icons/` (the file names are
  listed at the top of `gui/ui_kit/icons.py`).
- Group macros into **categories** (+ New Category, or right-click a macro);
  a category's switch turns all of its macros off in every profile.
- Click **Profile: …** at the top of the Macros page to switch profile.
- Devices, profiles, the abort key and appearance live in the **Settings** tab.
- **Input Visualizer** has a CPS tester that measures what an app actually
  receives (daemon -> kernel -> compositor -> app), a keyboard and mouse that
  light up as they are used, and a **Macro equivalent** readout that shows
  the code for what was just done (`tap(KEY_A)`, `combo(...)`, `wheel(2)`,
  `move_mouse(...)`, with waits).
- **Dictionary** lists every command, block, custom block and key name, with
  search. Click a command to see what it does; key names are drawn on a
  keyboard (hover for every alias).
- Held keys in the Input Visualizer show how long they've been held (to the
  millisecond), and a curved arrow under the mouse shows your last movement.
- **Game controllers** are picked up automatically (Settings > Devices):
  their buttons work in key combos (`BTN_SOUTH`, `BTN_TL`, ...), `getAxis("LX")`
  reads a stick or trigger, and with *Virtual controller* on, macros can
  press controller buttons and move sticks (`axis("LX", 1, time_=0.2)`).
- **OBS & replay overlay** (bottom of the Input Visualizer page):
  - *Expose Input Visualizer to OBS* serves the keyboard + mouse at
    `http://127.0.0.1:17380/`. Add it to OBS as a Browser Source, or press
    **Add to OBS** (needs OBS's WebSocket server: Tools > WebSocket Server
    Settings).
  - *Keyboard and mouse as separate pieces* splits that page in two
    (`:17383`, `:17384`); *Controller* is its own page (`:17385`).
  - *Simple input visualizer* (`:17381`) is a one-line list of what's held;
    its *Mouse movement* toggle adds an arrow-only page (`:17382`).
  - Your own input shows in orange, what macros press or move in blue.
  - **Customize…** changes colors, sizes, fonts, timers, the arrow and what's
    shown, with a live preview. Changes reach OBS immediately.
  - *Layered Replay Buffer* keeps your input in memory for as long as OBS's
    replay buffer, so afterglow can put the overlay on saved clips. The same
    look can be rendered by hand with `puppetry-overlay composite --clip
    clip.mp4 --clip-end <unix time> out.mp4`.

## Icon / logo

`assets/puppetry_small_logo.png` is the source for the app icon (taskbar,
search menu, alt-tab, etc.) -- it's resized at build time into the standard
`hicolor` icon-theme sizes (16px through 256px). `assets/puppetry_logo.png`
is the full detailed version, kept in the repo but not currently wired into
anything (nothing in this project needs a large logo yet).

To update either: replace the file at that same path, commit, push,
rebuild. Nothing else needs to change.

## Module options

| Option | Type | Description |
|---|---|---|
| `services.puppetry.enable` | bool | Enable the daemon, GUI, and udev/uinput setup. |
| `services.puppetry.user` | string | Username to grant `input`-group access to and run the per-user service as. |

## Trying it without committing to the module

```fish
nix run github:YOUR_GITHUB_USERNAME/puppetry
```

This runs just the GUI, useful for a quick look. It won't have the udev
rules or `uinput`/`input`-group access the module sets up, though, so the
daemon itself won't actually work until you install the module properly.

## Troubleshooting

**Nothing happens when I press my macro's combo.** Check
`journalctl --user -u macro-daemon -n 30 --no-pager` first. If the log shows
it watching a device named `macro-daemon-virtual-keyboard` or
`macro-daemon-virtual-mouse` as your "Keyboard"/"Mouse", it picked up its own
synthetic device instead of your real hardware -- open the editor and hit
**Detect** for the correct device.

**`move_to` / mouse position readout says "unavailable".** You're either not
on KDE Plasma, or `kdotool` isn't finding KWin's D-Bus interface. Confirm
`kdotool getmouselocation --shell` works in a plain terminal first.

## License

MIT -- see [LICENSE](LICENSE).
