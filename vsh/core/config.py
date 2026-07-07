import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pyaudio
from loguru import logger


@dataclass
class ShellConfig:
    inner_shell: str = ""
    voice_on_start: bool = False
    show_state_text: bool = True
    show_transcript: bool = True
    auto_submit: bool = False
    overlay_mode: str = "cursor"  # "cursor" (legacy corner HUD), "statusline" (reserved transparent line), "none"
    overlay_line: str = "bottom"  # "bottom" or "top" (statusline mode only)
    overlay_color: str = "36"  # ANSI color code for the statusline text


@dataclass
class KeybindConfig:
    toggle_listen: str = "ctrl+\\"
    toggle_listen_triggers: list[str] = field(
        default_factory=lambda: ["1c", "1b5b39323b3575", "1b5b39323b31333375", "1b5b32383b3575", "1b5b32383b31333375"]
    )


@dataclass
class ProviderConfig:
    provider: str = ""
    type: str = ""
    endpoint: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    url: str = ""
    command: str = ""
    format: str = "openai"
    response_path: str = ""
    device_index: int | None = None
    vad_threshold: int = 1000
    vad_silence_limit: int = 15
    output_mode: str = "speak_and_command"


@dataclass
class VshConfig:
    shell: ShellConfig = field(default_factory=ShellConfig)
    keybinds: KeybindConfig = field(default_factory=KeybindConfig)
    stt: ProviderConfig = field(default_factory=lambda: ProviderConfig("vosk"))
    tts: ProviderConfig = field(default_factory=lambda: ProviderConfig("supertonic"))
    llm: ProviderConfig = field(default_factory=ProviderConfig)
    custom_thinkers: dict = field(default_factory=dict)


def _get_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vsh" / "config.toml"


def get_audio_devices():
    from vsh.core.audio import no_stderr

    with no_stderr():
        try:
            p = pyaudio.PyAudio()
            devices = []
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    devices.append((i, info["name"]))
            p.terminate()
            return devices
        except Exception:
            return []


def capture_keybind():
    import os
    import termios
    import tty

    sys.stdout.write("\nPress the key combination you want to use to toggle the microphone...\n")
    sys.stdout.write("      (Press Enter or Esc to cancel)\n")
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 32)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if ch in (b"\r", b"\n", b"\x1b"):
        return None

    if ch == b"\x1c" or ch == b"\x1b[92;5u":
        return {
            "name": "ctrl+\\",
            "triggers": [
                b"\x1c".hex(),
                b"\x1b[92;5u".hex(),
                b"\x1b[92;133u".hex(),
                b"\x1b[28;5u".hex(),
                b"\x1b[28;133u".hex(),
            ],
            "bash": "\\C-\\",
            "zsh": "^\\\\",
            "fish": "\\c\\\\",
        }

    if ch == b"\x1d":
        triggers = [b"\x1d".hex(), b"\x1b[93;5u".hex(), b"\x1b[93;133u".hex()]
        return {"name": "ctrl+]", "triggers": triggers, "bash": "\\C-]", "zsh": "^]", "fish": "\\c]"}

    if ch == b"\x00":
        triggers = [b"\x00".hex(), b"\x1b[32;5u".hex(), b"\x1b[32;133u".hex()]
        return {"name": "ctrl+space", "triggers": triggers, "bash": "\\C-@", "zsh": "^@", "fish": "\\c@"}

    if len(ch) == 1 and 1 <= ch[0] <= 26 and ch[0] not in (9, 10, 13, 27):
        char = chr(ch[0] + ord("a") - 1)
        name = f"ctrl+{char}"
        triggers = [ch.hex(), f"\x1b[{ord(char)};5u".encode().hex(), f"\x1b[{ord(char)};133u".encode().hex()]
        return {
            "name": name,
            "triggers": triggers,
            "bash": f"\\C-{char}",
            "zsh": f"^{char.upper()}",
            "fish": f"\\c{char}",
        }

    if len(ch) == 1 and 32 <= ch[0] <= 126:
        char = chr(ch[0])
        hex_repr = ch.hex()
        return {"name": f"custom ('{char}')", "triggers": [hex_repr], "bash": None, "zsh": None, "fish": None}

    hex_repr = ch.hex()
    return {"name": f"custom ({hex_repr})", "triggers": [hex_repr], "bash": None, "zsh": None, "fish": None}


def update_shell_rc_bind(rc_file: str, keybind_data: dict | None, set_default: bool) -> bool:
    import re

    rc_path = Path(rc_file).expanduser()
    is_zsh = "zsh" in rc_file
    is_fish = "fish" in rc_file

    append_cmd = ""
    if keybind_data:
        name = keybind_data["name"]
        if is_zsh:
            b = keybind_data.get("zsh")
            if not b:
                sys.stdout.write(f"\nWarning: Could not auto-generate zsh binding for {name}.\n")
                return False
            append_cmd += f"bindkey -s '{b}' 'vsh --voice\\n'\n"
        elif is_fish:
            b = keybind_data.get("fish")
            if not b:
                sys.stdout.write(f"\nWarning: Could not auto-generate fish binding for {name}.\n")
                return False
            append_cmd += f"bind {b} 'vsh --voice; commandline -f repaint'\n"
        else:
            b = keybind_data.get("bash")
            if not b:
                sys.stdout.write(f"\nWarning: Could not auto-generate bash binding for {name}.\n")
                return False
            append_cmd += f'bind \'"{b}":"vsh --voice\\n"\'\n'

    if set_default:
        if is_fish:
            append_cmd += "if not set -q VSH_ACTIVE; and isatty 1\n    exec vsh\nend\n"
        else:
            append_cmd += 'if [ -z "$VSH_ACTIVE" ] && [ -t 1 ]; then\n    exec vsh\nfi\n'

    if not append_cmd:
        return True

    block_start = "# --- vsh configuration start ---"
    block_end = "# --- vsh configuration end ---"
    block = f"\n{block_start}\n{append_cmd}{block_end}\n"

    try:
        content = ""
        if rc_path.exists():
            content = rc_path.read_text()

        pattern = re.compile(f"\\n?{block_start}.*?{block_end}\\n?", re.DOTALL)
        if pattern.search(content):
            new_content = pattern.sub(lambda _: block, content)
        else:
            new_content = content.rstrip() + block

        rc_path.write_text(new_content)
        if keybind_data:
            sys.stdout.write(f"\nAdded shortcut {keybind_data['name']} to {rc_file}!\n")
        if set_default:
            sys.stdout.write(f"\nSet vsh as default shell in {rc_file}!\n")
        return True
    except Exception as e:
        sys.stdout.write(f"\nFailed to write shortcut: {e}\n")
        return False


def interactive_setup(section: str = None) -> None:
    """Prompt the user for configuration and write config.toml."""
    import os
    import shutil
    import sys

    # Lazy import: InquirerPy is only needed for the interactive wizard, not for
    # headless commands (stt/tts/wrap) or importing vsh modules in tests.
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    from vsh.core.config import _get_config_path, capture_keybind, get_audio_devices, update_shell_rc_bind

    cfg_path = _get_config_path()
    existing = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, "rb") as f:
                existing = tomllib.load(f)
        except Exception:
            pass

    def get_val(*keys, default):
        d = existing
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d

    sys.stdout.write(f"\nConfiguration Setup ({section or 'All'})\n")

    # --- SHELL / CORE ---
    if section in (None, "core", "shell"):
        default_shell = get_val(
            "shell",
            "inner_shell",
            default=os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh",
        )
        inner_shell = inquirer.text(message="Inner shell:", default=default_shell).execute()
        voice_on_start = inquirer.confirm(
            message="Enable voice automatically on start?", default=get_val("shell", "voice_on_start", default=False)
        ).execute()
        show_state_text = inquirer.confirm(
            message="Show state labels (Idle/Listening/Processing) next to the animation?",
            default=get_val("shell", "show_state_text", default=True),
        ).execute()
        show_transcript = inquirer.confirm(
            message="Show live transcription on screen while processing?",
            default=get_val("shell", "show_transcript", default=True),
        ).execute()
        auto_submit = inquirer.confirm(
            message="Auto-submit the LLM's commands? (Warning: skips manual review)",
            default=get_val("shell", "auto_submit", default=False),
        ).execute()
    else:
        inner_shell = get_val("shell", "inner_shell", default=os.environ.get("SHELL") or "/bin/sh")
        voice_on_start = get_val("shell", "voice_on_start", default=False)
        show_state_text = get_val("shell", "show_state_text", default=True)
        show_transcript = get_val("shell", "show_transcript", default=True)
        auto_submit = get_val("shell", "auto_submit", default=False)

    # --- LLM / THINKER ---
    model = get_val("llm", "model", default="")
    endpoint = get_val("llm", "endpoint", default="")
    api_key_env = get_val("llm", "api_key_env", default="")
    cli_cmd = get_val("llm", "command", default="")

    if section in (None, "llm", "thinker"):
        thinker = inquirer.select(
            message="Default LLM provider:",
            choices=[
                Choice("none", "None (Direct injection)"),
                Choice("ollama", "Ollama (Local LLM)"),
                Choice("custom_http", "Cloud API (OpenAI, Anthropic, Custom)"),
                Choice("custom_cli", "Custom CLI Tool (e.g., codex)"),
            ],
            default=get_val("llm", "provider", default="none"),
        ).execute()

        if thinker == "ollama":
            model = inquirer.text(message="Ollama model:", default=get_val("llm", "model", default="llama3")).execute()
        elif thinker == "custom_http":
            endpoint = inquirer.text(
                message="API Endpoint:",
                default=get_val("llm", "custom_http", "endpoint", default="https://api.openai.com/v1/chat/completions"),
            ).execute()
            api_key_env = inquirer.text(
                message="API Key Env Var:",
                default=get_val("llm", "custom_http", "api_key_env", default="OPENAI_API_KEY"),
            ).execute()
            model = inquirer.text(
                message="Model name:", default=get_val("llm", "custom_http", "model", default="gpt-4o-mini")
            ).execute()
        elif thinker == "custom_cli":
            cli_cmd = inquirer.text(
                message="CLI Command:", default=get_val("llm", "custom_cli", "command", default='codex exec "{}"')
            ).execute()

        output_mode = "speak_and_command"
        if thinker != "none":
            output_mode = inquirer.select(
                message="How should the LLM respond?",
                choices=[
                    Choice("speak_and_command", "Conversation & Commands (Default)"),
                    Choice("command_only", "Commands only"),
                    Choice("speak_only", "Conversation only (No terminal injection)"),
                ],
                default=get_val("llm", "output_mode", default="speak_and_command"),
            ).execute()
    else:
        thinker = get_val("llm", "provider", default="none")
        output_mode = get_val("llm", "output_mode", default="speak_and_command")
        if thinker == "custom_http":
            endpoint = get_val("llm", "custom_http", "endpoint", default="")
            api_key_env = get_val("llm", "custom_http", "api_key_env", default="")
            model = get_val("llm", "custom_http", "model", default="")
        elif thinker == "custom_cli":
            cli_cmd = get_val("llm", "custom_cli", "command", default="")

    # --- STT ---
    vosk_model_name = get_val("stt", "model", default="")
    vosk_model_url = get_val("stt", "url", default="")
    stt_http = {
        "endpoint": get_val("stt", "endpoint", default=""),
        "api_key_env": get_val("stt", "api_key_env", default=""),
        "format": get_val("stt", "format", default=""),
        "model": get_val("stt", "model", default=""),
    }

    if section in (None, "stt", "voice"):
        # Intelligently determine the UI default
        default_stt = get_val("stt", "provider", default="vosk")
        if default_stt == "custom_http" and "groq.com" in get_val("stt", "endpoint", default=""):
            default_stt = "groq"

        stt_provider = inquirer.select(
            message="Speech-to-Text (STT) provider:",
            choices=[
                Choice("vosk", "Vosk (Local, Offline, Fast)"),
                Choice("groq", "Groq Whisper (Cloud, Insanely Fast, Free)"),
                Choice("sarvam", "Sarvam AI (Cloud, Indian Languages)"),
                Choice("gcp", "Google Cloud STT (Cloud, High Accuracy)"),
                Choice("custom_http", "Custom API (OpenAI, Gemini, etc.)"),
            ],
            default=default_stt,
        ).execute()

        if stt_provider == "vosk":
            sys.stdout.write("\nFetching official Vosk model list...\n")
            sys.stdout.flush()
            import json
            import urllib.request

            try:
                with urllib.request.urlopen("https://alphacephei.com/vosk/models/model-list.json", timeout=5) as r:
                    models = json.loads(r.read().decode("utf-8"))
                choices = []
                for m in models:
                    if str(m.get("obsolete", "false")).lower() == "true":
                        continue
                    name = f"[{m.get('lang', '?')}] {m.get('lang_text', '?')} | {m.get('name', '?')} | {m.get('type', '?')} | {m.get('size_text', '?')}"
                    choices.append(Choice(m, name))
                selected_m = inquirer.fuzzy(
                    message="Search and select a Vosk model:", choices=choices, match_exact=True
                ).execute()
                if selected_m:
                    vosk_model_name = selected_m["name"]
                    vosk_model_url = selected_m["url"]
            except Exception as e:
                sys.stderr.write(f"\nFailed to fetch models: {e}\nUsing default.\n")
                vosk_model_name = get_val("stt", "model", default="vosk-model-en-in-0.5")
                vosk_model_url = get_val(
                    "stt", "url", default="https://alphacephei.com/vosk/models/vosk-model-en-in-0.5.zip"
                )
        elif stt_provider == "sarvam":
            stt_http["api_key_env"] = inquirer.text(
                message="Sarvam API Key Env Var:", default=get_val("stt", "api_key_env", default="SARVAM_API_KEY")
            ).execute()
        elif stt_provider == "groq":
            stt_http["endpoint"] = "https://api.groq.com/openai/v1/audio/transcriptions"
            stt_http["api_key_env"] = inquirer.text(
                message="Groq API Key Env Var:", default=get_val("stt", "api_key_env", default="GROQ_API_KEY")
            ).execute()
            stt_http["format"] = "openai_whisper"
            stt_http["model"] = "whisper-large-v3"
        elif stt_provider == "custom_http":
            stt_http["endpoint"] = inquirer.text(
                message="STT API Endpoint:",
                default=get_val("stt", "endpoint", default="https://api.openai.com/v1/audio/transcriptions"),
            ).execute()
            stt_http["api_key_env"] = inquirer.text(
                message="STT API Key Env Var:", default=get_val("stt", "api_key_env", default="OPENAI_API_KEY")
            ).execute()
            stt_http["format"] = inquirer.select(
                message="STT API Format:",
                choices=[Choice("openai_whisper", "OpenAI Whisper"), Choice("gemini", "Gemini Base64")],
                default=get_val("stt", "format", default="openai_whisper"),
            ).execute()
            stt_http["model"] = inquirer.text(
                message="STT Model name:", default=get_val("stt", "model", default="whisper-1")
            ).execute()
    else:
        stt_provider = get_val("stt", "provider", default="vosk")

    # --- TTS ---
    tts_http = {
        "endpoint": get_val("tts", "endpoint", default=""),
        "api_key_env": get_val("tts", "api_key_env", default=""),
        "format": get_val("tts", "format", default=""),
        "model": get_val("tts", "model", default=""),
    }

    if section in (None, "tts", "voice"):
        tts_provider = inquirer.select(
            message="Text-to-Speech (TTS) provider:",
            choices=[
                Choice("supertonic", "Supertonic (Local, Offline)"),
                Choice("polly", "AWS Polly (Cloud, Fast)"),
                Choice("sarvam", "Sarvam AI (Cloud, Indian Languages)"),
                Choice("custom_http", "Cloud API (OpenAI TTS, ElevenLabs, etc.)"),
                Choice("none", "None (Disable Voice Output)"),
            ],
            default=get_val("tts", "provider", default="supertonic"),
        ).execute()

        if tts_provider == "sarvam":
            tts_http["api_key_env"] = inquirer.text(
                message="Sarvam API Key Env Var:", default=get_val("tts", "api_key_env", default="SARVAM_API_KEY")
            ).execute()
            tts_http["model"] = inquirer.select(
                message="Sarvam Voice:",
                choices=[Choice("priya", "Priya"), Choice("aditya", "Aditya")],
                default=get_val("tts", "model", default="priya"),
            ).execute()
        elif tts_provider == "custom_http":
            tts_http["endpoint"] = inquirer.text(
                message="TTS API Endpoint:",
                default=get_val("tts", "endpoint", default="https://api.openai.com/v1/audio/speech"),
            ).execute()
            tts_http["api_key_env"] = inquirer.text(
                message="TTS API Key Env Var:", default=get_val("tts", "api_key_env", default="OPENAI_API_KEY")
            ).execute()
            tts_http["format"] = inquirer.select(
                message="TTS API Format:",
                choices=[Choice("openai_tts", "OpenAI TTS"), Choice("elevenlabs", "ElevenLabs")],
                default=get_val("tts", "format", default="openai_tts"),
            ).execute()
            tts_http["model"] = inquirer.text(
                message="TTS Model name:", default=get_val("tts", "model", default="tts-1")
            ).execute()
        elif tts_provider == "polly":
            tts_http["model"] = inquirer.select(
                message="AWS Polly Voice:",
                choices=[Choice("Matthew", "Matthew"), Choice("Joanna", "Joanna")],
                default=get_val("tts", "model", default="Matthew"),
            ).execute()
    else:
        tts_provider = get_val("tts", "provider", default="supertonic")

    # --- DEVICE ---
    if section in (None, "device"):
        devices = get_audio_devices()
        device_choices = [Choice(None, "Default System Mic")] + [Choice(d[0], f"[{d[0]}] {d[1]}") for d in devices]
        device_index = inquirer.select(
            message="Input microphone:", choices=device_choices, default=get_val("stt", "device_index", default=None)
        ).execute()
    else:
        device_index = get_val("stt", "device_index", default=None)

    # --- KEYBINDS ---
    keybind_data = {
        "name": get_val("keybinds", "toggle_listen", default="ctrl+\\"),
        "triggers": get_val("keybinds", "toggle_listen_triggers", default=[b"\x1c".hex(), b"\x1b[92;5u".hex()]),
        "bash": "\\C-\\",
        "zsh": "^\\\\",
    }

    add_shortcut = False
    set_default = False

    if section in (None, "keybind", "keybinds"):
        if inquirer.confirm(message="Set a custom keybind to toggle the microphone?", default=False).execute():
            while True:
                kb = capture_keybind()
                if not kb:
                    break
                if inquirer.confirm(message=f"You pressed {kb['name']}. Use this keybind?", default=True).execute():
                    keybind_data = kb
                    break
        sys.stdout.write(f"Selected keybind: {keybind_data['name']}\n")

        add_shortcut = inquirer.confirm(
            message="Add a global shell shortcut to launch vsh on demand?", default=False
        ).execute()
        set_default = inquirer.confirm(message="Set vsh as your default interactive shell?", default=False).execute()

        if add_shortcut or set_default:
            default_rc = "~/.bashrc"
            if "fish" in inner_shell:
                default_rc = "~/.config/fish/config.fish"
            elif "zsh" in inner_shell:
                default_rc = "~/.zshrc"
            rc_file = inquirer.text(message="Shell config file to update:", default=default_rc).execute()
            update_shell_rc_bind(rc_file, keybind_data if add_shortcut else None, set_default)

    # --- WRITE OUT TOML ---
    import json

    lines = [
        "[shell]",
        f"inner_shell = {json.dumps(inner_shell)}",
        f"voice_on_start = {str(voice_on_start).lower()}",
        f"show_state_text = {str(show_state_text).lower()}",
        f"show_transcript = {str(show_transcript).lower()}",
        f"auto_submit = {str(auto_submit).lower()}",
        "",
        "[keybinds]",
        f"toggle_listen = {json.dumps(keybind_data['name'])}",
        f"toggle_listen_triggers = {json.dumps(keybind_data['triggers'])}",
        "",
        "[stt]",
    ]

    if stt_provider in ("custom_http", "groq"):
        lines.extend(
            [
                'provider = "custom_http"',
                'type = "http"',
                f"endpoint = {json.dumps(stt_http['endpoint'])}",
                f"api_key_env = {json.dumps(stt_http['api_key_env'])}",
                f"format = {json.dumps(stt_http['format'])}",
                f"model = {json.dumps(stt_http['model'])}",
            ]
        )
    elif stt_provider == "sarvam":
        lines.extend(['provider = "sarvam"', f"api_key_env = {json.dumps(stt_http['api_key_env'])}"])
    elif stt_provider == "vosk":
        lines.extend(['provider = "vosk"'])
        if vosk_model_name:
            lines.extend([f"model = {json.dumps(vosk_model_name)}", f"url = {json.dumps(vosk_model_url)}"])
    else:
        lines.extend([f'provider = "{stt_provider}"'])

    if device_index is not None:
        lines.append(f"device_index = {device_index}")

    lines.extend(["", "[tts]"])
    if tts_provider == "custom_http":
        lines.extend(
            [
                'provider = "custom_http"',
                'type = "http"',
                f"endpoint = {json.dumps(tts_http['endpoint'])}",
                f"api_key_env = {json.dumps(tts_http['api_key_env'])}",
                f"format = {json.dumps(tts_http['format'])}",
                f"model = {json.dumps(tts_http['model'])}",
            ]
        )
    elif tts_provider == "sarvam":
        lines.extend(['provider = "sarvam"', f"api_key_env = {json.dumps(tts_http['api_key_env'])}"])
        if tts_http.get("model"):
            lines.extend([f"model = {json.dumps(tts_http['model'])}"])
    elif tts_provider == "polly":
        lines.extend(['provider = "polly"'])
        if tts_http.get("model"):
            lines.extend([f"model = {json.dumps(tts_http['model'])}"])
    else:
        lines.extend([f'provider = "{tts_provider}"'])

    if thinker and thinker != "none":
        lines.extend(["", "[llm]", f'output_mode = "{output_mode}"'])
        if thinker == "custom_http":
            lines.extend(
                [
                    'provider = "custom_http"',
                    "",
                    "[llm.custom_http]",
                    'type = "http"',
                    f"endpoint = {json.dumps(endpoint)}",
                    f"api_key_env = {json.dumps(api_key_env)}",
                    'format = "openai"',
                    f"model = {json.dumps(model)}",
                ]
            )
        elif thinker == "custom_cli":
            lines.extend(
                ['provider = "custom_cli"', "", "[llm.custom_cli]", 'type = "cli"', f"command = {json.dumps(cli_cmd)}"]
            )
        else:
            lines.extend([f'provider = "{thinker}"'])
            if model:
                lines.extend([f"model = {json.dumps(model)}"])

    # Merge custom thinkers if they exist
    if "llm" in existing:
        for k, v in existing["llm"].items():
            if isinstance(v, dict) and k not in ("custom_http", "custom_cli"):
                lines.extend(["", f"[llm.{k}]"])
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, bool):
                        lines.append(f"{sub_k} = {str(sub_v).lower()}")
                    elif isinstance(sub_v, int):
                        lines.append(f"{sub_k} = {sub_v}")
                    else:
                        lines.append(f"{sub_k} = {json.dumps(sub_v)}")

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(lines) + "\n")
    sys.stdout.write(f"\nConfiguration successfully written to {cfg_path}\n")


def load_config() -> VshConfig:
    """Load configuration from file, then apply environment variable overrides."""
    config_path = _get_config_path()
    cfg = VshConfig()

    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)

            for k, v in data.items():
                if k == "llm":
                    for sub_k, sub_v in v.items():
                        if isinstance(sub_v, dict):
                            cfg.custom_thinkers[sub_k] = dict(sub_v)
                        elif hasattr(cfg.llm, sub_k):
                            setattr(cfg.llm, sub_k, sub_v)
                elif hasattr(cfg, k):
                    vars(getattr(cfg, k)).update(v)

        except Exception as e:
            logger.error(f"Failed to load {config_path}: {e}")

        # Resolve api_key_env -> api_key for each custom thinker profile
        for profile in cfg.custom_thinkers.values():
            if "api_key_env" in profile:
                profile["api_key"] = os.environ.get(profile["api_key_env"], "")

        # Resolve api_key_env for core providers
        for block in (cfg.stt, cfg.tts, cfg.llm):
            if block.api_key_env and not block.api_key:
                block.api_key = os.environ.get(block.api_key_env, "")

    # Environment overrides
    if "VSH_SHELL" in os.environ:
        cfg.shell.inner_shell = os.environ["VSH_SHELL"]

    if "VSH_VOICE" in os.environ:
        val = os.environ["VSH_VOICE"].lower()
        cfg.shell.voice_on_start = val in ("1", "true", "yes", "on")

    if "VSH_LLM" in os.environ:
        cfg.llm.provider = os.environ["VSH_LLM"]

    if "VSH_LLM_KEY" in os.environ:
        cfg.llm.api_key = os.environ["VSH_LLM_KEY"]

    if "VSH_OVERLAY" in os.environ:
        val = os.environ["VSH_OVERLAY"].lower()
        if val in ("none", "cursor", "statusline"):
            cfg.shell.overlay_mode = val
        elif val in ("0", "off", "false"):
            cfg.shell.overlay_mode = "none"

    config_path = _get_config_path()
    if not config_path.exists():
        interactive_setup()
        return load_config()

    return cfg
