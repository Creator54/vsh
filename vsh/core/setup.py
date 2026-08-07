import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pyaudio

from vsh.core.config import _get_config_path


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


def get_default_rc(shell_path: str) -> str:
    name = os.path.basename(shell_path or "").lower()
    if "fish" in name:
        return "~/.config/fish/config.fish"
    if "zsh" in name:
        return "~/.zshrc"
    if "oil" in name or "osh" in name:
        return "~/.config/oil/oshrc"
    if "sh" in name and "bash" not in name:
        return "~/.profile"
    return "~/.bashrc"


def update_shell_rc_bind(rc_file: str, keybind_data: dict | None, set_default: bool) -> bool:
    rc_path = Path(rc_file).expanduser()
    rc_name = os.path.basename(rc_file).lower()
    is_zsh = "zsh" in rc_name
    is_fish = "fish" in rc_name

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
            append_cmd += (
                'if isatty 1; and begin; not set -q VSH_ACTIVE_TTY; or test "$VSH_ACTIVE_TTY" != (tty); end\n'
                "    exec vsh\n"
                "end\n"
            )
        else:
            append_cmd += 'if [ -t 1 ] && [ "${VSH_ACTIVE_TTY:-}" != "$(tty)" ]; then\n    exec vsh\nfi\n'

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
        elif set_default:
            new_content = block.lstrip() + ("\n" + content.lstrip() if content else "")
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


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        raise TypeError("TOML does not support null values")
    return json.dumps(value)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _dump_toml(data: dict) -> str:
    """Serialize the simple tables and scalar values used by VSH config."""
    lines: list[str] = []

    def emit_table(path: tuple[str, ...], table: dict) -> None:
        if path:
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")

        for key, value in table.items():
            if not isinstance(value, dict) and value is not None:
                lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")

        for key, value in table.items():
            if isinstance(value, dict):
                emit_table((*path, str(key)), value)

    emit_table((), data)
    return "\n".join(lines) + "\n"


def update_keybind_config(config_path: Path, keybind_data: dict) -> None:
    """Update only the keybind table, preserving the rest of the file."""
    with config_path.open("rb") as stream:
        data = tomllib.load(stream)

    keybinds = dict(data.get("keybinds", {}))
    keybinds["toggle_listen"] = keybind_data["name"]
    keybinds["toggle_listen_triggers"] = keybind_data["triggers"]

    lines = config_path.read_text().splitlines()
    output = []
    index = 0
    in_keybinds = False
    replaced = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "[keybinds]":
            output.append("[keybinds]")
            output.extend(f"{key} = {_toml_value(value)}" for key, value in keybinds.items())
            replaced = True
            in_keybinds = True
            index += 1
            continue
        if in_keybinds:
            if stripped.startswith("[") and stripped.endswith("]"):
                in_keybinds = False
                output.append(line)
            index += 1
            continue
        output.append(line)
        index += 1

    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append("[keybinds]")
        output.extend(f"{key} = {_toml_value(value)}" for key, value in keybinds.items())

    config_path.write_text("\n".join(output) + "\n")


def interactive_setup(section: str | None = None) -> None:
    """Prompt the user for configuration and write config.toml."""
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

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
        auto_submit = inquirer.confirm(
            message="Run the AI's commands automatically? (Warning: skips manual review)",
            default=get_val("shell", "auto_submit", default=False),
        ).execute()
    else:
        inner_shell = get_val("shell", "inner_shell", default=os.environ.get("SHELL") or "/bin/sh")
        voice_on_start = get_val("shell", "voice_on_start", default=False)
        auto_submit = get_val("shell", "auto_submit", default=False)

    model = get_val("llm", "model", default="")
    endpoint = get_val("llm", "endpoint", default="")
    api_key_env = get_val("llm", "api_key_env", default="")
    cli_cmd = get_val("llm", "command", default="")

    if section in (None, "llm", "thinker"):
        thinker = inquirer.select(
            message="Default AI provider:",
            choices=[
                Choice("none", "None (use recognized speech directly)"),
                Choice("ollama", "Ollama (local)"),
                Choice("custom_http", "Cloud provider (OpenAI, Anthropic, custom)"),
                Choice("custom_cli", "Command-line tool (for example, codex)"),
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
                message="How should the AI respond?",
                choices=[
                    Choice("speak_and_command", "Conversation and commands (default)"),
                    Choice("command_only", "Commands only"),
                    Choice("speak_only", "Conversation only (do not run commands)"),
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

    vosk_model_name = get_val("stt", "model", default="")
    vosk_model_url = get_val("stt", "url", default="")
    stt_http = {
        "endpoint": get_val("stt", "endpoint", default=""),
        "api_key_env": get_val("stt", "api_key_env", default=""),
        "format": get_val("stt", "format", default=""),
        "model": get_val("stt", "model", default=""),
    }

    if section in (None, "stt", "voice"):
        default_stt = get_val("stt", "provider", default="vosk")
        if default_stt == "custom_http" and "groq.com" in get_val("stt", "endpoint", default=""):
            default_stt = "groq"

        stt_provider = inquirer.select(
            message="Speech recognition provider:",
            choices=[
                Choice("vosk", "Vosk (local, offline)"),
                Choice("groq", "Groq Whisper (cloud)"),
                Choice("sarvam", "Sarvam AI (cloud, Indian languages)"),
                Choice("gcp", "Google Cloud Speech-to-Text"),
                Choice("custom_http", "Custom provider (OpenAI, Gemini, etc.)"),
            ],
            default=default_stt,
        ).execute()

        if stt_provider == "vosk":
            sys.stdout.write("\nFetching official Vosk model list...\n")
            sys.stdout.flush()
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
                sys.stderr.write(f"\nFailed to fetch official Vosk model list: {e}\n")
                vosk_model_name = inquirer.text(
                    message="Vosk Model Name:", default=get_val("stt", "model", default="")
                ).execute()
                vosk_model_url = inquirer.text(
                    message="Vosk Model Download URL:", default=get_val("stt", "url", default="")
                ).execute()
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

    tts_http = {
        "endpoint": get_val("tts", "endpoint", default=""),
        "api_key_env": get_val("tts", "api_key_env", default=""),
        "format": get_val("tts", "format", default=""),
        "model": get_val("tts", "model", default=""),
    }

    if section in (None, "tts", "voice"):
        tts_provider = inquirer.select(
            message="Voice output provider:",
            choices=[
                Choice("supertonic", "Supertonic (local, offline)"),
                Choice("polly", "AWS Polly (cloud)"),
                Choice("sarvam", "Sarvam AI (cloud, Indian languages)"),
                Choice("custom_http", "Custom provider (OpenAI, ElevenLabs, etc.)"),
                Choice("none", "None (disable voice output)"),
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

    if section in (None, "device"):
        devices = get_audio_devices()
        device_choices = [Choice(None, "Default System Mic")] + [Choice(d[0], f"[{d[0]}] {d[1]}") for d in devices]
        device_index = inquirer.select(
            message="Input microphone:", choices=device_choices, default=get_val("stt", "device_index", default=None)
        ).execute()
    else:
        device_index = get_val("stt", "device_index", default=None)

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
            default_rc = get_default_rc(inner_shell)
            rc_file = inquirer.text(message="Shell config file to update:", default=default_rc).execute()
            update_shell_rc_bind(rc_file, keybind_data if add_shortcut else None, set_default)

    def table(name: str) -> dict:
        value = existing.get(name)
        if not isinstance(value, dict):
            value = {}
            existing[name] = value
        return value

    table("shell").update(
        {
            "inner_shell": inner_shell,
            "voice_on_start": voice_on_start,
            "auto_submit": auto_submit,
        }
    )
    table("keybinds").update(
        {
            "toggle_listen": keybind_data["name"],
            "toggle_listen_triggers": keybind_data["triggers"],
        }
    )

    stt = table("stt")
    stt["provider"] = "custom_http" if stt_provider == "groq" else stt_provider
    if stt_provider in ("custom_http", "groq"):
        stt.update({"type": "http", **stt_http})
    elif stt_provider == "sarvam":
        stt["api_key_env"] = stt_http["api_key_env"]
    elif stt_provider == "vosk" and vosk_model_name:
        stt.update({"model": vosk_model_name, "url": vosk_model_url})
    if device_index is None:
        stt.pop("device_index", None)
    else:
        stt["device_index"] = device_index

    tts = table("tts")
    tts["provider"] = tts_provider
    if tts_provider == "custom_http":
        tts.update({"type": "http", **tts_http})
    elif tts_provider == "sarvam":
        tts["api_key_env"] = tts_http["api_key_env"]
        if tts_http.get("model"):
            tts["model"] = tts_http["model"]
    elif tts_provider == "polly" and tts_http.get("model"):
        tts["model"] = tts_http["model"]

    llm = table("llm")
    llm["provider"] = thinker
    llm["output_mode"] = output_mode
    if thinker == "custom_http":
        profile = llm.get("custom_http")
        if not isinstance(profile, dict):
            profile = {}
            llm["custom_http"] = profile
        profile.update(
            {
                "type": "http",
                "endpoint": endpoint,
                "api_key_env": api_key_env,
                "format": "openai",
                "model": model,
            }
        )
    elif thinker == "custom_cli":
        profile = llm.get("custom_cli")
        if not isinstance(profile, dict):
            profile = {}
            llm["custom_cli"] = profile
        profile.update({"type": "cli", "command": cli_cmd})
    elif thinker == "ollama" and model:
        llm["model"] = model

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_dump_toml(existing))
    sys.stdout.write(f"\nConfiguration successfully written to {cfg_path}\n")
