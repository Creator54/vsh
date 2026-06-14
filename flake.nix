{
  description = "vsh: Voice Shell - Offline STT and TTS orchestrator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = import ./shell.nix { inherit pkgs; };

        packages.default = pkgs.writeShellScriptBin "vsh" ''
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.portaudio pkgs.alsa-lib ]}:$LD_LIBRARY_PATH"
          
          # ponytail: nix-run flake source is read-only, point uv to a writable persistent venv
          export UV_PROJECT_ENVIRONMENT="''${XDG_CACHE_HOME:-$HOME/.cache}/vsh/venv"
          export UV_PYTHON="${pkgs.python311}/bin/python"
          
          exec ${pkgs.uv}/bin/uv run --project ${./.} python -m vsh.main "$@"
        '';

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/vsh";
        };
      }
    );
}
