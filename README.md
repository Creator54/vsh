# vsh: Voice Shell

An interactive voice-controlled terminal wrapper with pluggable STT/TTS and LLM integration.

## Features
- **PTY Shell**: Control bash/zsh/fish via voice.
- **LLM Integrations**: Generate commands and ask questions using Ollama, OpenAI, Anthropic, or custom CLI scripts.
- **Pluggable Audio**:
  - **STT (Speech-to-Text):** Local (Vosk), Cloud (Google Cloud), or custom HTTP APIs (Whisper/Sarvam/Gemini).
  - **TTS (Text-to-Speech):** Local (Supertonic), Cloud (AWS Polly), or custom HTTP APIs (ElevenLabs/OpenAI).

## Installation

Requires `portaudio` and `alsa-lib` (Linux).

```bash
# Via UV (Recommended)
uv tool install git+https://github.com/creator54/vsh.git

# Via Nix Profile
nix profile install github:creator54/vsh
```
*(For local development: `uv tool install -e .`)*

## Usage

**Core Commands:**
- `vsh setup` : First-time wizard to configure your LLM, microphone, and initial keybind.
- `vsh bind` : Update your microphone toggle keybind and inject it into your shell config.
- `vsh [--voice]` : Start the voice-controlled shell (`--verbose` for logs, `--echo` to print transcripts).
- `vsh stt [--file <audio.wav>]` : Speech-to-text utility (records mic if no file provided).
- `vsh tts "<text>" [--save <out.wav>] [--stream]` : Text-to-speech utility.

**Environment Overrides:**
`VSH_SHELL`, `VSH_VOICE`, `VSH_LLM`, and `VSH_LLM_KEY` override defaults from `~/.config/vsh/config.toml`.

**Keybind Notes:**
- **Terminal Protocols**: Standard terminals often drop modifiers for symbols or squash legacy keys together. To fix this in Kitty, explicitly map them in your `kitty.conf`:
  - *`Ctrl+,`* : `map ctrl+, send_text all \x1b[44;5u`
  - *`Ctrl+Backspace`* : `map ctrl+backspace send_text all \x1b[127;5u`
- **Linux Intercepts**: Linux catches some signals (like `Ctrl+O` or `Ctrl+S`) before your shell does.
  - *Fix*: Unbind it in your OS (e.g., add `stty discard undef` to `.bashrc` to free up `Ctrl+O`).
