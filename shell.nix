{ pkgs ? import <nixpkgs> {} }:

let
  voskSmall = pkgs.fetchzip {
    url = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip";
    sha256 = "sha256-CIoPZ/krX+UW2w7c84W3oc1n4zc9BBS/fc8rVYUthuY=";
  };
  voskBig = pkgs.fetchzip {
    url = "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip";
    sha256 = "sha256-CIoPZ/krX+UW2w7c84W3oc1n4zc9BBS/fc8rVYUthuY="; # placeholder
  };
in
pkgs.mkShell {
  buildInputs = with pkgs; [ portaudio alsa-lib pkg-config python311 uv ];

  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.portaudio}/lib:${pkgs.alsa-lib}/lib:$LD_LIBRARY_PATH"
    export C_INCLUDE_PATH="${pkgs.portaudio}/include"
    export LIBRARY_PATH="${pkgs.portaudio}/lib"
    
    # ponytail: symlink models for local access
    mkdir -p models
    ln -sfn ${voskSmall} models/vosk-model-small-en-us-0.15
    # ln -sfn {voskBig} models/vosk-model-en-us-0.22 # uncomment if big model needed

    if [ ! -d ".venv" ]; then uv venv; fi
    uv sync --quiet
  '';
}
