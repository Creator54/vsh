# vsh: Voice Shell

A Speech-to-Text and Text-to-Speech orchestrator, and an interactive voice-controlled shell wrapper. STT and TTS run fully offline; LLM providers can optionally use remote APIs (e.g. OpenAI). `vsh` allows you to seamlessly integrate LLMs and voice commands into your daily terminal workflow.

## Features
- **Interactive PTY Shell**: Wrap your normal shell (bash/zsh/fish) and execute commands via voice.
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
| **Synthesize** | `vsh tts "text" [--save out.wav] [--stream]` | Convert text to spoken audio. `--stream` outputs raw bytes to stdout. |

### Additional Flags

| Flag | Applies To | Description |
| :--- | :--- | :--- |
| `--echo` | `vsh` (interactive) | Run in diagnostic echo mode without LLMs. |
| `--verbose` / `-v` | `vsh` (interactive) | Enable verbose logging. |
| `--stream` | `vsh tts` | Output raw audio bytes to stdout instead of playing through speakers. |

### Environment Variable Overrides

These override the corresponding values from `~/.config/vsh/config.toml`:

| Variable | Description |
| :--- | :--- |
| `VSH_SHELL` | Inner shell to run (e.g. `/bin/zsh`). |
| `VSH_VOICE` | Enable/disable voice on startup (`true`/`false`). |
| `VSH_LLM` | LLM provider name (e.g. `ollama`, `openai`). |
| `VSH_LLM_KEY` | API key for the configured LLM provider. |

*Note: STT/TTS models download automatically on first run (~400MB total).*
