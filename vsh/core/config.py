import contextlib
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pyaudio
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from loguru import logger


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
    device_index: int | None = None
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


@contextlib.contextmanager
def no_alsa_errors():
    with open(os.devnull, "w") as devnull:
        old_stderr = os.dup(sys.stderr.fileno())
        os.dup2(devnull.fileno(), sys.stderr.fileno())
        try:
            yield
        finally:
            os.dup2(old_stderr, sys.stderr.fileno())
            os.close(old_stderr)


def get_audio_devices():
    with no_alsa_errors():
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
    inner_shell = inquirer.text(message="Inner shell:", default=default_shell).execute()

    voice_on_start = inquirer.confirm(message="Enable voice automatically on start?", default=False).execute()

    thinker = inquirer.select(
        message="Select the default thinker (LLM) provider:",
        choices=[
            Choice("none", "None (Direct shell injection)"),
            Choice("ollama", "Ollama (Local LLM)"),
            Choice("http", "HTTP API (OpenAI, Anthropic, Custom)"),
            Choice("cli", "Custom CLI Tool (e.g., codex)"),
        ],
        default="none",
    ).execute()

    model = ""
    endpoint = ""
    api_key_env = ""
    cli_cmd = ""

    if thinker == "ollama":
        model = inquirer.text(message="Ollama model:", default="llama3").execute()
    elif thinker == "http":
        endpoint = inquirer.text(
            message="API Endpoint:", default="https://api.openai.com/v1/chat/completions"
        ).execute()
        api_key_env = inquirer.text(message="API Key Env Var:", default="OPENAI_API_KEY").execute()
        model = inquirer.text(message="Model name:", default="gpt-4o-mini").execute()
    elif thinker == "cli":
        cli_cmd = inquirer.text(message="CLI Command:", default='codex exec "{}"').execute()

    devices = get_audio_devices()
    device_choices = [Choice(None, "Default System Mic")] + [Choice(d[0], f"[{d[0]}] {d[1]}") for d in devices]
    device_index = inquirer.select(message="Select your input device:", choices=device_choices, default=None).execute()

    keybind = inquirer.text(message="Shortcut key (toggle microphone):", default="Ctrl+\\").execute()
    add_shortcut = inquirer.confirm(
        message="Add a global shortcut to your shell config to launch vsh on demand?", default=True
    ).execute()

    if add_shortcut:
        if "fish" in inner_shell or "fish" in default_shell:
            default_rc = "~/.config/fish/config.fish"
        elif "zsh" in inner_shell or "zsh" in default_shell:
            default_rc = "~/.zshrc"
        else:
            default_rc = "~/.bashrc"

        rc_file = inquirer.text(message="Shell config file:", default=default_rc).execute()

        is_zsh = "zsh" in rc_file
        is_fish = "fish" in rc_file

        if keybind.lower() in ("ctrl+\\", "ctrl+\\\\"):
            if is_zsh:
                bind_str = "^\\\\"  # -> ^\\
            elif is_fish:
                bind_str = "\\c\\\\"  # -> \c\\
            else:
                bind_str = "\\C-\\"  # -> \C-\
        elif keybind.lower() == "ctrl+v":
            if is_zsh:
                bind_str = "^V"
            elif is_fish:
                bind_str = "\\cv"
            else:
                bind_str = "\\C-v"
        else:
            bind_str = keybind

        if is_zsh:
            append_cmd = f"bindkey -s '{bind_str}' 'vsh --voice\\n'"
        elif is_fish:
            append_cmd = f"bind '{bind_str}' 'commandline \"vsh --voice\"; commandline -f execute'"
        else:
            append_cmd = f'bind \'"{bind_str}":"vsh --voice\\n"\''

        block_start = "# --- vsh configuration start ---"
        block_end = "# --- vsh configuration end ---"
        block = f"\n{block_start}\n{append_cmd}\n{block_end}\n"

        rc_path = Path(rc_file).expanduser()
        try:
            import re

            content = ""
            if rc_path.exists():
                content = rc_path.read_text()

            pattern = re.compile(f"\\n?{block_start}.*?{block_end}\\n?", re.DOTALL)
            if pattern.search(content):
                new_content = pattern.sub(lambda _: block, content)
            else:
                new_content = content.rstrip() + block

            rc_path.write_text(new_content)
            sys.stdout.write(f"\n[vsh] Added shortcut {keybind} to {rc_file}!\n")
        except Exception as e:
            sys.stdout.write(f"\n[vsh] Failed to write shortcut: {e}\n")

    import json

    lines = [
        "[shell]",
        f"inner_shell = {json.dumps(inner_shell)}",
        f"voice_on_start = {'true' if voice_on_start else 'false'}",
        "",
        "[keybinds]",
        f"toggle_listen = {json.dumps(keybind)}",
        "",
        "[stt]",
        'provider = "vosk"',
    ]
    if device_index is not None:
        lines.append(f"device_index = {device_index}")

    lines.extend(
        [
            "",
            "[tts]",
            'provider = "supertonic"',
        ]
    )

    if thinker and thinker != "none":
        lines.extend(["", "[llm]"])

        if thinker == "http":
            lines.extend(['provider = "custom_http"', "", "[llm.custom_http]"])
            lines.extend(
                [
                    'type = "http"',
                    f"endpoint = {json.dumps(endpoint)}",
                    f"api_key_env = {json.dumps(api_key_env)}",
                    'format = "openai"',
                    f"model = {json.dumps(model)}",
                ]
            )
        elif thinker == "cli":
            lines.extend(['provider = "custom_cli"', "", "[llm.custom_cli]"])
            lines.extend(
                [
                    'type = "cli"',
                    f"command = {json.dumps(cli_cmd)}",
                ]
            )
        else:
            lines.extend([f'provider = "{thinker}"'])
            if model:
                lines.extend([f"model = {json.dumps(model)}"])

    lines.extend(
        [
            "",
            "# You can define additional custom profiles here.",
            "# Example HTTP API:",
            "# [llm.openai]",
            '# type = "http"',
            '# endpoint = "https://api.openai.com/v1/chat/completions"',
            '# api_key_env = "OPENAI_API_KEY"',
            '# format = "openai"',
            '# model = "gpt-4o-mini"',
            "#",
            "# Example CLI Tool:",
            "# [llm.codex]",
            '# type = "cli"',
            '# command = "codex -p"',
        ]
    )

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
        return load_config()

    return cfg
