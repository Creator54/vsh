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

          # uv_project_environment points to a writable cache path; nix store is read-only
          export UV_PROJECT_ENVIRONMENT="''${XDG_CACHE_HOME:-$HOME/.cache}/vsh/venv"
          export UV_PYTHON="${pkgs.python311}/bin/python"
          # unset any outer VIRTUAL_ENV so uv does not see a mismatch
          unset VIRTUAL_ENV

          exec ${pkgs.uv}/bin/uv run --project ${./.} python -m vsh.main "$@"
        '';

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/vsh";
        };
      }
    );
}
