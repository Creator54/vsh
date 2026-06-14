# vsh: Voice Shell

100% offline Speech-to-Text and Text-to-Speech orchestrator.

## Features
- **STT:** [Vosk](https://github.com/alphacep/vosk-api) (Offline)
- **TTS:** [Supertonic](https://github.com/supertonic-tts/supertonic-python-sdk) (Offline)
- **Signal:** Direct Text ↔ Audio transformation with universal resampling.
- **Reproducible:** Nix-native development and execution.

## Setup
Requires `portaudio` and `alsa-lib`.

### Nix (Recommended)
```bash
nix run github:creator54/vsh -- duplex
```

## Usage
### Global Options
- `-v, --verbose`: Show internal state logs.
- `--in INDEX`: Select input device index.
- `--out INDEX`: Select output device index.
- `--vad-thr INT`: VAD energy threshold (default: 800).
- `--vad-sil INT`: VAD silence limit in chunks (default: 20).

### Commands
| Command | Options | Description |
| :--- | :--- | :--- |
| `vsh duplex` | `--voice [F1-5, M1-5]` | Audio ↔ Text echo loop |
| `vsh stt` | `-f, --file PATH`, `--rate INT`, `--width INT` | Audio → Text (live, file, or stdin) |
| `vsh tts` | `TEXT`, `--save PATH`, `--voice NAME`, `--stream` | Text → Audio (arg or stdin) |
| `vsh list-devices` | | List local hardware indices |

### Examples
```bash
# Discover local audio hardware indices
vsh list-devices

# Transcribe from a pipe with custom rate
cat audio.raw | vsh stt --file - --rate 44100

# Synthesize to stdout
echo "hello" | vsh tts --stream | aplay -r 44100 -f f32le

# Save synthesis to file
vsh tts "Local synthesis" --save out.wav --voice M1

# Use specific hardware with high VAD threshold
vsh --in 3 --out 6 --vad-thr 1500 duplex
```
