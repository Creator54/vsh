# vsh: Voice Shell

A 100% offline Speech-to-Text and Text-to-Speech orchestrator, and an interactive voice-controlled shell wrapper. `vsh` allows you to seamlessly integrate LLMs and voice commands into your daily terminal workflow.

## Features
- **Interactive PTY Shell**: Wrap your normal shell (bash/zsh) and execute commands via voice.
- **LLM Integration**: Ask questions and generate commands via built-in providers (Ollama, OpenAI, custom scripts).
- **STT**: [Vosk](https://github.com/alphacep/vosk-api) (Offline)
- **TTS**: [Supertonic](https://github.com/supertonic-tts/supertonic-python-sdk) (Offline)
- **VAD**: Energy-based silence detection via `numpy`.

## Installation
Requires `portaudio` and `alsa-lib` (on Linux).

### Global Installation (Recommended)
**Via Nix Profile:**
```bash
nix profile install github:creator54/vsh
```

**Via UV Tool:**
```bash
uv tool install git+https://github.com/creator54/vsh.git
```

### Local Development (From Clone)
If you have cloned the repository locally and want to install it:

**With Nix:**
```bash
nix develop
nix run . -- setup
# Or to install globally from local clone: nix profile install .
```

**With UV:**
```bash
uv tool install -e .
# Then run: vsh setup
```

## Usage

| Mode | Command | Description |
| :--- | :--- | :--- |
| **Interactive Shell** | `vsh [--voice]` | Starts the voice-controlled terminal wrapper. |
| **Setup Wizard** | `vsh setup` | Interactive prompt to configure LLMs, microphones, and shell keybinds. |
| **Transcribe** | `vsh stt [--file audio.wav]` | Convert mic or WAV file audio to text (`stdout`). |
| **Synthesize** | `vsh tts "text" [--save out.wav]` | Convert text to spoken audio. |

*Note: STT/TTS models download automatically on first run (~400MB total).*
