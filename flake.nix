{
  description = "Puppetry -- C++ keyboard/mouse macro daemon + Qt (PySide6) editor for NixOS (KDE Plasma / Hyprland, Wayland)";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      guiPython = pkgs.python3.withPackages (ps: [ ps.evdev ps.pyside6 ]);
    in
    {
      # Add this to your own flake's inputs, then:
      #   imports = [ inputs.puppetry.nixosModules.default ];
      #   services.puppetry = { enable = true; user = "yourusername"; };
      nixosModules.default = import ./module.nix;

      # NOT evaluated against a real `nix build`/`nix run` this session
      # (no `nix` binary in the sandbox this was written in) -- see
      # HANDOFF_README.md. Treat as a careful draft.
      #
      # Lets you also just build/run the GUI directly without the full
      # module, e.g. `nix run github:YOUR_GITHUB_USERNAME/puppetry` --
      # handy for a quick look before committing to the NixOS module
      # route. This standalone path won't have the udev rules / uinput
      # group access the module sets up, so the daemon itself (not just
      # the GUI) still needs the module installed to actually work.
      packages.${system} = {
        default = self.packages.${system}.puppetry;

        puppetry-daemon = pkgs.stdenv.mkDerivation {
          pname = "puppetry-daemon";
          version = "2.0";
          src = ./native;
          nativeBuildInputs = [ pkgs.cmake pkgs.pkg-config pkgs.python3 ];
          buildInputs = [ pkgs.python3 ];
          doCheck = true;
          checkPhase = "ctest --output-on-failure -LE timing";
          # (default cmake install: puppetry-daemon + puppetry-transcribe)
        };

        puppetry = pkgs.stdenv.mkDerivation {
          pname = "puppetry";
          version = "2.0";
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.makeWrapper pkgs.imagemagick ];
          installPhase =
            let
              iconSizes = [ 16 24 32 48 64 128 256 ];
            in
            ''
              mkdir -p $out/bin $out/share/puppetry
              cp -r ${./gui}/* $out/share/puppetry/
              makeWrapper ${guiPython}/bin/python3 $out/bin/puppetry \
                --set PYTHONPATH $out/share/puppetry \
                --set PUPPETRY_BIN_DIR ${self.packages.${system}.puppetry-daemon}/bin \
                --add-flags $out/share/puppetry/app.py

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
