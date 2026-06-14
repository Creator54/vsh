{ pkgs ? import <nixpkgs> {} }:

let
  voskIN = pkgs.fetchzip {
    url = "https://alphacephei.com/vosk/models/vosk-model-en-in-0.5.zip";
    sha256 = "sha256-sE7NkBHP7sHRyyqPIkLxNuf2aZqNeNZVpSrBVYafWSU=";
  };
in
pkgs.mkShell {
  buildInputs = with pkgs; [ portaudio alsa-lib pkg-config python311 uv ];

  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.portaudio}/lib:${pkgs.alsa-lib}/lib:$LD_LIBRARY_PATH"
    export C_INCLUDE_PATH="${pkgs.portaudio}/include"
    export LIBRARY_PATH="${pkgs.portaudio}/lib"
    
    # ponytail: symlink model for local access
    mkdir -p models
    ln -sfn ${voskIN} models/vosk-model-en-in-0.5

    # unset any outer VIRTUAL_ENV so uv does not see a mismatch
    unset VIRTUAL_ENV
    if [ ! -d ".venv" ]; then uv venv && uv sync --quiet; fi
  '';
}
