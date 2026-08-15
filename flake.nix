{
  description = "Puppetry -- keyboard/mouse macro daemon + GTK4 editor for NixOS (KDE Plasma / Hyprland, Wayland)";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      # Add this to your own flake's inputs, then:
      #   imports = [ inputs.puppetry.nixosModules.default ];
      #   services.puppetry = { enable = true; user = "yourusername"; };
      nixosModules.default = import ./module.nix;

      # Lets you also just build/run the GUI directly without the
      # full module, e.g. `nix run github:YOUR_GITHUB_USERNAME/puppetry`
      # -- handy for a quick look before committing to the NixOS
      # module route. Note this standalone path won't have the
      # udev rules / uinput group access the module sets up, so the
      # daemon itself (not just the GUI) still needs the module
      # installed to actually work.
      packages.${system} = {
        default = self.packages.${system}.puppetry;

        puppetry = pkgs.stdenv.mkDerivation {
          pname = "puppetry";
          version = "1.0";
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.makeWrapper pkgs.wrapGAppsHook4 pkgs.imagemagick ];
          buildInputs = [ pkgs.gtk4 pkgs.gobject-introspection pkgs.adwaita-icon-theme pkgs.hicolor-icon-theme ];
          installPhase =
            let
              guiPython = pkgs.python3.withPackages (ps: [ ps.evdev ps.pygobject3 ]);
              iconSizes = [ 16 24 32 48 64 128 256 ];
            in
            ''
              mkdir -p $out/bin $out/share/puppetry
              cp ${./macro_daemon.py} $out/share/puppetry/macro_daemon.py
              cp ${./macro_gui.py} $out/share/puppetry/macro_gui.py
              makeWrapper ${guiPython}/bin/python3 $out/bin/puppetry \
                --set PYTHONPATH $out/share/puppetry \
                --add-flags $out/share/puppetry/macro_gui.py

              ${pkgs.lib.concatMapStringsSep "\n" (sz: ''
                mkdir -p $out/share/icons/hicolor/${toString sz}x${toString sz}/apps
                convert ${./assets/puppetry_small_logo.png} -resize ${toString sz}x${toString sz} \
                  $out/share/icons/hicolor/${toString sz}x${toString sz}/apps/puppetry.png
              '') iconSizes}
            '';
        };
      };

      apps.${system}.default = {
        type = "app";
        program = "${self.packages.${system}.puppetry}/bin/puppetry";
      };
    };
}
