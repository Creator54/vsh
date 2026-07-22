# vsh: Voice Shell

An interactive voice-controlled terminal wrapper with pluggable STT/TTS and LLM integration.

## Features

- Control Bash, Zsh, or Fish by voice.
- Use Ollama, OpenAI, Anthropic, or any CLI command as the LLM.
- Pick local, cloud, or custom HTTP speech providers.

## Installation

Requires `portaudio` and `alsa-lib` (Linux).

```bash
# uv
uv tool install git+https://github.com/creator54/vsh.git

# Nix
nix profile install github:creator54/vsh
```

- Local checkout: `uv tool install -e .`

## Usage

- `vsh`: start the shell.
  - `--voice`: start listening immediately.
  - `--verbose`: show logs.
  - `--echo`: return transcripts without an LLM.
  - `--serve --port 8770`: expose the live shell over loopback HTTP.
- `vsh setup`: configure the LLM, microphone, and keybind.
- `vsh bind`: change the microphone keybind.
- `vsh stt [--file <audio.wav>]`: transcribe the microphone or a WAV file.
- `vsh tts "<text>" [--save <out.wav>] [--stream]`: speak or save text.

## Voice replies

- Format: `{"speech":"Opening it.","command":"cd ~/project"}`
  - Use `null` when there is no command.
  - Invalid JSON is shown as text and never run.
- Speech comes first.
  - TTS available: play it.
  - TTS off or failed: print it.
- Command comes next.
  - `auto_submit = true`: run it.
  - `auto_submit = false`: leave it editable.

## Environment overrides

- Shell and voice: `VSH_SHELL`, `VSH_VOICE`.
- LLM: `VSH_LLM`, `VSH_LLM_KEY`.
- Output: `VSH_OUTPUT_MODE` (`speak_and_command`, `command_only`, or `speak_only`).
- Visual: `VSH_OVERLAY` (`auto`, `kitty`, or `none`).
- Voice command: `VSH_VOICE_HANDLER='command {}'`.
- Fish replies: `VSH_RESPONSE_BRIDGE=fish-signal`.

## Keybinds

- Kitty may need explicit mappings for modified symbols:
  - `Ctrl+,`: `map ctrl+, send_text all \x1b[44;5u`
  - `Ctrl+Backspace`: `map ctrl+backspace send_text all \x1b[127;5u`
- The terminal driver may reserve control keys such as `Ctrl+O` or `Ctrl+S`.
  - Example: `stty discard undef` frees `Ctrl+O`.
