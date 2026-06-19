import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pyaudio
from loguru import logger
from prompt_toolkit import prompt
from prompt_toolkit.shortcuts import radiolist_dialog, yes_no_dialog


@dataclass
class ShellConfig:
    inner_shell: str = ""
    voice_on_start: bool = False


@dataclass
class KeybindConfig:
    toggle_listen: str = "ctrl+\\"


@dataclass
class ProviderConfig:
    provider: str = ""
    type: str = ""
    endpoint: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    command: str = ""
    format: str = "openai"
    response_path: str = ""
    device_index: int = None
    vad_threshold: int = 1000
    vad_silence_limit: int = 15


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

def interactive_setup() -> None:
    """Prompt the user for first-time configuration and write config.toml."""
    sys.stdout.write("\n[vsh] First-time setup\n")

    default_shell = os.environ.get("SHELL", "/bin/bash")
    inner_shell = prompt(f"Inner shell [{default_shell}]: ") or default_shell

    voice_on_start = yes_no_dialog(
        title="Voice on Start",
        text="Enable voice automatically on start?"
    ).run()

    thinker = radiolist_dialog(
        title="Thinker Provider",
        text="Select the default thinker (LLM) provider:",
        values=[
            ("none", "None (Direct shell injection)"),
            ("echo", "Echo (Mock/Test)"),
            ("ollama", "Ollama (Local LLM)"),
        ]
    ).run()

    model = ""
    if thinker == "ollama":
        model = prompt("Ollama model [llama3]: ") or "llama3"

    devices = get_audio_devices()
    device_values = [(None, "Default System Mic")] + devices
    device_index = radiolist_dialog(
        title="Microphone",
        text="Select your input device:",
        values=device_values
    ).run()

    add_shortcut = yes_no_dialog(
        title="Shell Shortcut",
        text="Would you like to add a global shortcut to your shell config (.bashrc/.zshrc) to launch vsh on demand?"
    ).run()

    if add_shortcut:
        default_rc = "~/.zshrc" if "zsh" in default_shell else "~/.bashrc"
        rc_file = prompt(f"Shell config file [{default_rc}]: ") or default_rc
        keybind = prompt("Shortcut key [Ctrl+\\]: ") or "Ctrl+\\"

        is_zsh = "zsh" in rc_file
        if keybind.lower() in ("ctrl+\\", "ctrl+\\\\"):
            bind_str = "^\\" if is_zsh else "\\C-\\"
        elif keybind.lower() == "ctrl+v":
            bind_str = "^V" if is_zsh else "\\C-v"
        else:
            bind_str = keybind

        append_cmd = f"\n# vsh hotkey\nbindkey -s '{bind_str}' 'vsh --voice\\n'\n" if is_zsh else f"\n# vsh hotkey\nbind '\"{bind_str}\":\"vsh --voice\\n\"'\n"
        rc_path = Path(rc_file).expanduser()
        try:
            with open(rc_path, "a") as f:
                f.write(append_cmd)
            sys.stdout.write(f"\n[vsh] Added shortcut {keybind} to {rc_file}!\n")
        except Exception as e:
            sys.stdout.write(f"\n[vsh] Failed to write shortcut: {e}\n")

    lines = [
        '[shell]',
        f'inner_shell = "{inner_shell}"',
        f'voice_on_start = {"true" if voice_on_start else "false"}',
        '',
        '[keybinds]',
        'toggle_listen = "ctrl+\\\\"',
        '',
        '[stt]',
        'provider = "vosk"',
    ]
    if device_index is not None:
        lines.append(f'device_index = {device_index}')

    lines.extend([
        '',
        '[tts]',
        'provider = "supertonic"',
    ])

    if thinker and thinker != "none":
        lines.extend([
            '',
            '[llm]',
            f'provider = "{thinker}"',
        ])
        if model:
            lines.extend([
                f'model = "{model}"',
            ])

    lines.extend([
        '',
        '# Built-in thinkers: echo, ollama',
        '# Custom profiles (define any [llm.<name>] section):',
        '# [llm.openai]',
        '# type = "http"',
        '# endpoint = "https://api.openai.com/v1/chat/completions"',
        '# api_key_env = "OPENAI_API_KEY"',
        '# format = "openai"',
        '# model = "gpt-4o-mini"',
        '#',
        '# [llm.codex]',
        '# type = "cli"',
        '# command = "codex -p"',
    ])

    config_path = _get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n")
    sys.stdout.write(f"\n[vsh] Config saved to {config_path}\n\n")

def load_config() -> VshConfig:
    """Load configuration from file, then apply environment variable overrides."""
    config_path = _get_config_path()
    cfg = VshConfig()

    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)

            for k, v in data.items():
                if k.startswith("llm."):
                    profile_name = k[4:]
                    cfg.custom_thinkers[profile_name] = dict(v)
                elif hasattr(cfg, k):
                    vars(getattr(cfg, k)).update(v)

        except Exception as e:
            logger.error(f"Failed to load {config_path}: {e}")

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

    config_path = _get_config_path()
    if not config_path.exists():
        interactive_setup()
        cfg = VshConfig()

    return cfg
