# Puppetry

A keyboard/mouse macro daemon for NixOS, with a GTK4 editor. Watches your
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
    puppetry.url = "github:mr-tinkle-winkle/puppetry";
    # Optional but recommended -- makes puppetry reuse YOUR pinned
    # nixpkgs instead of fetching its own copy:
    puppetry.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, puppetry, ... }: {
    nixosConfigurations.yourhostname = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        inputs.puppetry.nixosModules.default
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

Then rebuild.

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
nix run github:mr-tinkle-winkle/puppetry
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
