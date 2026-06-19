{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [ portaudio alsa-lib pkg-config python311 uv ];

  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.portaudio}/lib:${pkgs.alsa-lib}/lib:$LD_LIBRARY_PATH"
    export C_INCLUDE_PATH="${pkgs.portaudio}/include"
    export LIBRARY_PATH="${pkgs.portaudio}/lib"

    # unset any outer VIRTUAL_ENV so uv does not see a mismatch
    unset VIRTUAL_ENV
    if [ ! -d ".venv" ]; then uv venv && uv sync --quiet; fi
  '';
}
