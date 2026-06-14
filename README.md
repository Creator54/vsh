# vsh: Voice Shell

100% offline Speech-to-Text and Text-to-Speech orchestrator.

## Features
- **STT:** [Vosk](https://github.com/alphacep/vosk-api) (Offline)
- **TTS:** [Supertonic](https://github.com/supertonic-tts/supertonic-python-sdk) (Offline)
- **VAD:** Energy-based silence detection via stdlib `audioop`.
- **Reproducible:** Nix-native development and execution.

## Setup
Requires `portaudio` and `alsa-lib`.

### Nix (Recommended)
```bash
nix run github:creator54/vsh -- duplex
```

### Local Development
```bash
nix develop # or nix-shell
uv sync
```

## Usage
| Mode | Command |
| :--- | :--- |
| **Full Loop** | `vsh duplex` |
| **Transcribe** | `vsh stt [--file audio.wav]` |
| **Synthesize** | `vsh tts "text" [--save out.wav]` |

*Note: Models download automatically on first run (~400MB total).*
