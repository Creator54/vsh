# vsh: Voice Shell

A 100% offline Speech-to-Text and Text-to-Speech orchestrator, and an interactive voice-controlled shell wrapper. `vsh` allows you to seamlessly integrate LLMs and voice commands into your daily terminal workflow.

## Features
- **Interactive PTY Shell**: Wrap your normal shell (bash/zsh) and execute commands via voice.
- **LLM Integration**: Ask questions and generate commands via built-in providers (Ollama, OpenAI, custom scripts).
- **STT**: [Vosk](https://github.com/alphacep/vosk-api) (Offline)
- **TTS**: [Supertonic](https://github.com/supertonic-tts/supertonic-python-sdk) (Offline)
- **VAD**: Energy-based silence detection via `numpy`.

## Setup
Requires `portaudio` and `alsa-lib` (on Linux).

### Nix (Recommended)
```bash
nix run github:creator54/vsh -- setup
```

### Local Development
```bash
nix develop # or nix-shell
uv sync
```

## Usage

| Mode | Command | Description |
| :--- | :--- | :--- |
| **Interactive Shell** | `vsh [--voice]` | Starts the voice-controlled terminal wrapper. |
| **Setup Wizard** | `vsh setup` | Interactive prompt to configure LLMs, microphones, and shell keybinds. |
| **Transcribe** | `vsh stt [--file audio.wav]` | Convert mic or WAV file audio to text (`stdout`). |
| **Synthesize** | `vsh tts "text" [--save out.wav]` | Convert text to spoken audio. |

*Note: STT/TTS models download automatically on first run (~400MB total).*
