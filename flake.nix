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
          export VSH_ORIGINAL_PATH="$PATH"
          export VSH_ORIGINAL_VIRTUAL_ENV="''${VIRTUAL_ENV-}"
          export VSH_ORIGINAL_UV_PROJECT_ENVIRONMENT="''${UV_PROJECT_ENVIRONMENT-}"
          export VSH_ORIGINAL_UV_PYTHON="''${UV_PYTHON-}"
          export VSH_ORIGINAL_C_INCLUDE_PATH="''${C_INCLUDE_PATH-}"
          export VSH_ORIGINAL_LIBRARY_PATH="''${LIBRARY_PATH-}"
          export VSH_ORIGINAL_LD_LIBRARY_PATH="''${LD_LIBRARY_PATH-}"
          export VSH_NIX_WRAPPER=1
          export PATH="${pkgs.lib.makeBinPath [ pkgs.stdenv.cc pkgs.pkg-config ]}:$PATH"
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.portaudio pkgs.alsa-lib ]}:$LD_LIBRARY_PATH"
          export C_INCLUDE_PATH="${pkgs.portaudio}/include''${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"
          export LIBRARY_PATH="${pkgs.portaudio}/lib''${LIBRARY_PATH:+:$LIBRARY_PATH}"

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
