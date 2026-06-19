# vsh: Voice Shell

An offline voice-controlled terminal wrapper with STT/TTS and LLM integration.

## Features
- **PTY Shell**: Control bash/zsh/fish via voice.
- **LLM**: Generate commands and ask questions (Ollama, OpenAI, custom scripts).
- **Offline Audio**: Local STT ([Vosk](https://github.com/alphacep/vosk-api)) and TTS ([Supertonic](https://github.com/supertonic-tts/supertonic-python-sdk)).

## Installation

Requires `portaudio` and `alsa-lib` (Linux). Models download automatically on first run (~400MB).

```bash
# Via UV (Recommended)
uv tool install git+https://github.com/creator54/vsh.git

# Via Nix Profile
nix profile install github:creator54/vsh
```
*(For local development: `uv tool install -e .` or `nix run . -- setup`)*

## Usage

Start by configuring your LLMs, microphone, and keybinds:
```bash
vsh setup
```

**Core Commands:**
- `vsh [--voice]` : Start the voice-controlled shell. Options: `--verbose`, `--echo`.
- `vsh stt [--file audio.wav]` : Convert audio to text (`stdout`).
- `vsh tts "text" [--save out.wav] [--stream]` : Synthesize speech.

**Environment Overrides:**
`VSH_SHELL`, `VSH_VOICE`, `VSH_LLM`, and `VSH_LLM_KEY` override defaults from `~/.config/vsh/config.toml`.
