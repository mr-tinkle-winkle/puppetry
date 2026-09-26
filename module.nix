# Puppetry -- NixOS module
#
# UPDATED THIS SESSION: the daemon is now a C++ binary (native/) instead
# of the pure-Python macro_daemon.py, and the editor is PySide6 (Qt)
# instead of GTK4 -- see HANDOFF_README.md for what's verified vs not.
# This file has NOT been evaluated against a real `nixos-rebuild`/`nix
# build` (no `nix` binary in the sandbox this was written in) -- treat
# the derivations below as a careful draft, not a proven-working
# package, and do a real `nixos-rebuild build --flake .` before trusting
# it on a machine.
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

  # The daemon: CMake + a C++17 compiler + embedded CPython (via
  # python3-embed through pkg-config) + pthreads. No GUI toolkit
  # dependency at all now -- that's entirely on the GUI derivation
  # below, and the two processes only ever talk over the control
  # socket + shared JSON config files, same as the old Python
  # daemon/GTK GUI pair did.
  puppetryDaemon = pkgs.stdenv.mkDerivation {
    pname = "puppetry-daemon";
    version = "2.0";
    src = ./native;
    nativeBuildInputs = [ pkgs.cmake pkgs.pkg-config pkgs.python3 ];
    buildInputs = [ pkgs.python3 ];
    # tools/gen_keycodes.py reads THIS BUILD's own
    # linux/input-event-codes.h at build time (see that script's
    # docstring for why this isn't a committed static table) --
    # glibc's kernel headers package provides it in the sandboxed
    # build environment same as any other libc header.
    cmakeFlags = [ "-DCMAKE_BUILD_TYPE=Release" ];
    doCheck = true;
    checkPhase = ''
      ctest --output-on-failure
    '';
    installPhase = ''
      mkdir -p $out/bin
      cp puppetry-daemon $out/bin/puppetry-daemon
    '';
  };

  # The GUI: PySide6 + python-evdev (combo recorder) + the C++
  # daemon's own CLI contract (--name=.../--abort), all still through
  # `import`able plain files rather than a wheel -- same "ship the
  # scripts declaratively" approach the old module used, just for a
  # bigger file set (app.py, puppetry_config.py, combo_recorder.py,
  # gui/ui_kit/**).
  guiPython = pkgs.python3.withPackages (ps: [ ps.evdev ps.pyside6 ]);

  puppetryIconSizes = [ 16 24 32 48 64 128 256 ];

  puppetryGui = pkgs.stdenv.mkDerivation {
    pname = "puppetry";
    version = "2.0";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.makeWrapper pkgs.imagemagick ];
    installPhase = ''
      mkdir -p $out/share/puppetry
      cp -r ${./gui}/* $out/share/puppetry/
      mkdir -p $out/bin
      makeWrapper ${guiPython}/bin/python3 $out/bin/puppetry \
        --set PYTHONPATH $out/share/puppetry \
        --add-flags $out/share/puppetry/app.py

      ${lib.concatMapStringsSep "\n" (sz: ''
        mkdir -p $out/share/icons/hicolor/${toString sz}x${toString sz}/apps
        convert ${./assets/puppetry_small_logo.png} -resize ${toString sz}x${toString sz} \
          $out/share/icons/hicolor/${toString sz}x${toString sz}/apps/puppetry.png
      '') puppetryIconSizes}
    '';
  };

  puppetryDesktopItem = pkgs.makeDesktopItem {
    name = "puppetry";
    exec = "puppetry";
    icon = "puppetry";
    desktopName = "Puppetry";
    comment = "Configure keyboard/mouse macros";
    categories = [ "Utility" ];
    # Qt's own default WM_CLASS derivation is used now (no PyGObject
    # application_id to match anymore) -- confirm this still resolves
    # correctly under real KWin/Hyprland alt-tab/launcher lookups; it's
    # untested (see HANDOFF_README.md).
    startupWMClass = "puppetry";
  };
in
{
  options.services.puppetry = {
    enable = lib.mkEnableOption "the Puppetry macro daemon and its Qt editor";

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
    boot.kernelModules = [ "uinput" ];

    services.udev.extraRules = ''
      KERNEL=="uinput", MODE="0660", GROUP="input", TAG+="uaccess"
      SUBSYSTEM=="input", ATTRS{name}=="macro-daemon-virtual-mouse", ENV{ID_INPUT_JOYSTICK}="", ENV{ID_INPUT_MOUSE}="1"
    '';

    users.users.${cfg.user}.extraGroups = [ "input" ];

    environment.systemPackages = [
      puppetryGui
      puppetryDesktopItem
      pkgs.kdotool
    ];

    systemd.user.services.macro-daemon = {
      description = "Puppetry macro daemon";
      wantedBy = [ "default.target" ];
      path = [ pkgs.kdotool ];
      serviceConfig = {
        ExecStart = "${puppetryDaemon}/bin/puppetry-daemon";
        Restart = "on-failure";
        RestartSec = 2;
      };
    };
  };
}
