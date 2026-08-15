# Puppetry -- NixOS module
#
# Exposes services.puppetry.* options. See this repo's README.md for
# the full setup walkthrough; short version:
#
#   imports = [ inputs.puppetry.nixosModules.default ];
#   services.puppetry = {
#     enable = true;
#     user = "yourusername";   # who gets input-device access + runs the service
#   };
#
# Then: sudo nixos-rebuild switch, log out and back in (group
# membership needs a fresh session), and either run `puppetry` or
# find "Puppetry" in your app launcher/search menu.

{ config, lib, pkgs, ... }:

let
  cfg = config.services.puppetry;

  # evdev is all the background daemon needs.
  daemonPython = pkgs.python3.withPackages (ps: [ ps.evdev ]);

  # The GUI additionally needs PyGObject (gi) to talk to GTK4.
  guiPython = pkgs.python3.withPackages (ps: [ ps.evdev ps.pygobject3 ]);

  # wrapGAppsHook4 walks the full runtime closure of buildInputs and
  # sets GI_TYPELIB_PATH / XDG_DATA_DIRS / etc. correctly on its own --
  # far more reliable than hand-listing typelib packages.
  #
  # Also installs the icon here (into the standard hicolor theme
  # layout) rather than as a bare file elsewhere -- icon lookup by
  # name (as used in the .desktop entry below) walks
  # share/icons/hicolor/<size>/apps/<name>.<ext> under each
  # XDG_DATA_DIRS entry, so it has to live at exactly this path to be
  # found at all.
  #
  # assets/puppetry_small_logo.png is the source for every size here
  # (16px up to 256px) -- it's the simplified/high-contrast variant
  # designed to stay legible small, and even a 256px app-grid tile is
  # "small" next to the level of detail in the full logo. Resized at
  # build time with ImageMagick rather than committing a pile of
  # pre-scaled duplicates to the repo.
  puppetryIconSizes = [ 16 24 32 48 64 128 256 ];

  puppetryGui = pkgs.stdenv.mkDerivation {
    pname = "puppetry";
    version = "1.0";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.makeWrapper pkgs.wrapGAppsHook4 pkgs.imagemagick ];
    buildInputs = [ pkgs.gtk4 pkgs.gobject-introspection pkgs.adwaita-icon-theme pkgs.hicolor-icon-theme ];
    installPhase = ''
      mkdir -p $out/bin
      makeWrapper ${guiPython}/bin/python3 $out/bin/puppetry \
        --set PYTHONPATH /etc/macro-daemon \
        --add-flags /etc/macro-daemon/macro_gui.py

      ${lib.concatMapStringsSep "\n" (sz: ''
        mkdir -p $out/share/icons/hicolor/${toString sz}x${toString sz}/apps
        convert ${./assets/puppetry_small_logo.png} -resize ${toString sz}x${toString sz} \
          $out/share/icons/hicolor/${toString sz}x${toString sz}/apps/puppetry.png
      '') puppetryIconSizes}
    '';
  };

  # A proper .desktop entry, as its own package output rather than a
  # bare /etc file -- app launchers (KRunner, Hyprland's
  # wofi/rofi-likes) only scan $XDG_DATA_DIRS/applications, which on
  # NixOS resolves to /run/current-system/sw/share/applications --
  # i.e. wherever environment.systemPackages gets aggregated to. A
  # file dropped at /etc/xdg/applications (an earlier version of this
  # module did exactly that) is NOT on that path and silently never
  # shows up in any launcher.
  puppetryDesktopItem = pkgs.makeDesktopItem {
    name = "puppetry";
    exec = "puppetry";
    icon = "puppetry";
    desktopName = "Puppetry";
    comment = "Configure keyboard/mouse macros";
    categories = [ "Utility" ];
    startupWMClass = "org.puppetry.Puppetry";  # must match MacroApp's
                                                 # application_id in
                                                 # macro_gui.py -- see
                                                 # comment there for why
                                                 # it has to be dotted.
  };
in
{
  options.services.puppetry = {
    enable = lib.mkEnableOption "the Puppetry macro daemon and its GTK4 editor";

    user = lib.mkOption {
      type = lib.types.str;
      example = "max";
      description = ''
        Username to grant input-device access to, and who the
        per-user systemd service runs for. Puppetry watches your
        real keyboard/mouse (read-only) and needs to be in the
        "input" group to do that.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # uinput isn't loaded by default on most kernels -- needed to
    # create the virtual output device the daemon writes synthetic
    # input to.
    boot.kernelModules = [ "uinput" ];

    # /dev/uinput defaults to root-only. This opens it to the "input"
    # group instead.
    #
    # The second rule fixes a real (and non-obvious) classification
    # bug: udev's built-in input_id heuristic tags our virtual mouse
    # device as ID_INPUT_JOYSTICK instead of ID_INPUT_MOUSE, which
    # makes Hyprland/KDE's libinput silently ignore its movement/click
    # events even though the daemon is emitting them correctly. This
    # is the same override pattern used for other virtual/uinput
    # devices (e.g. VR controllers, Steam Controller) that hit the
    # same heuristic quirk. It must sort after udev's own
    # classification rules (60-*) to take effect, which NixOS's
    # extraRules already guarantees by numbering this file higher.
    services.udev.extraRules = ''
      KERNEL=="uinput", MODE="0660", GROUP="input", TAG+="uaccess"
      SUBSYSTEM=="input", ATTRS{name}=="macro-daemon-virtual-mouse", ENV{ID_INPUT_JOYSTICK}="", ENV{ID_INPUT_MOUSE}="1"
    '';

    # Real keyboard/touchpad/mouse event nodes (/dev/input/eventN) are
    # already group "input" by default on NixOS -- this is what
    # actually grants read access to them.
    users.users.${cfg.user}.extraGroups = [ "input" ];

    # Ship both scripts declaratively -- `nixos-rebuild switch` is the
    # only thing that ever updates them. macro_gui.py imports
    # macro_daemon.py from the same directory at runtime, so both need
    # to land in /etc/macro-daemon/ together.
    environment.etc = {
      "macro-daemon/macro_daemon.py".source = ./macro_daemon.py;
      "macro-daemon/macro_gui.py".source = ./macro_gui.py;
    };

    environment.systemPackages = [
      puppetryGui
      puppetryDesktopItem
      pkgs.kdotool  # move_mouse(..., move_to=True) and the editor's
                    # live "Mouse position" readout both shell out to
                    # this -- it's how KWin's cursor position gets
                    # read on Wayland at all (there's no other
                    # portable way). Requires a KDE Plasma/KWin
                    # session; Hyprland users can still use everything
                    # else, move_to will just fall back to its
                    # corner-anchored path every time.
    ];

    # Runs as a per-user service (not system-wide) -- starts with the
    # user's login session, on either Hyprland or Plasma, and dies
    # with it. That's the right lifetime: it only ever needs to run
    # while someone's actually at the machine using input devices.
    systemd.user.services.macro-daemon = {
      description = "Puppetry macro daemon";
      wantedBy = [ "default.target" ];
      path = [ pkgs.kdotool ];  # systemd user services don't reliably
                                 # inherit the interactive shell's
                                 # PATH, so this makes it explicit.
      serviceConfig = {
        ExecStart = "${daemonPython}/bin/python3 /etc/macro-daemon/macro_daemon.py";
        Restart = "on-failure";
        RestartSec = 2;
        Environment = "PYTHONUNBUFFERED=1";
      };
    };
  };
}
