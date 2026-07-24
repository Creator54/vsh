import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


@dataclass
class ShellConfig:
    inner_shell: str = ""
    voice_on_start: bool = False
    auto_submit: bool = False
    overlay_mode: str = "auto"
    voice_handler: str = ""
    response_bridge: str = ""


@dataclass
class KeybindConfig:
    toggle_listen: str = "ctrl+\\"
    toggle_listen_triggers: list[str] = field(
        default_factory=lambda: [
            "1c",
            "1d",
            "1b5b39323b3575",
            "1b5b39323b31333375",
            "1b5b32383b3575",
            "1b5b32383b31333375",
        ]
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


def interactive_setup(section: str | None = None) -> None:
    from vsh.core.setup import interactive_setup as run_setup

    run_setup(section)


def load_config() -> VshConfig:
    """Load the config file, then apply environment overrides."""
    config_path = _get_config_path()
    cfg = VshConfig()

    if config_path.exists():
        try:
            with config_path.open("rb") as stream:
                data = tomllib.load(stream)

            for key, value in data.items():
                if key == "llm":
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, dict):
                            cfg.custom_thinkers[sub_key] = dict(sub_value)
                        elif hasattr(cfg.llm, sub_key):
                            setattr(cfg.llm, sub_key, sub_value)
                elif hasattr(cfg, key):
                    vars(getattr(cfg, key)).update(value)
        except Exception as error:
            logger.error(f"Failed to load {config_path}: {error}")

        for profile in cfg.custom_thinkers.values():
            if "api_key_env" in profile:
                profile["api_key"] = os.environ.get(profile["api_key_env"], "")

        for provider in (cfg.stt, cfg.tts, cfg.llm):
            if provider.api_key_env and not provider.api_key:
                provider.api_key = os.environ.get(provider.api_key_env, "")

    if "VSH_SHELL" in os.environ:
        cfg.shell.inner_shell = os.environ["VSH_SHELL"]

    if "VSH_VOICE" in os.environ:
        cfg.shell.voice_on_start = os.environ["VSH_VOICE"].lower() in ("1", "true", "yes", "on")

    if "VSH_LLM" in os.environ:
        cfg.llm.provider = os.environ["VSH_LLM"]

    if "VSH_LLM_KEY" in os.environ:
        cfg.llm.api_key = os.environ["VSH_LLM_KEY"]

    if "VSH_OUTPUT_MODE" in os.environ:
        mode = os.environ["VSH_OUTPUT_MODE"].lower()
        if mode in ("speak_and_command", "command_only", "speak_only"):
            cfg.llm.output_mode = mode

    if "VSH_OVERLAY" in os.environ:
        overlay = os.environ["VSH_OVERLAY"].lower()
        if overlay in ("auto", "kitty", "none"):
            cfg.shell.overlay_mode = overlay
        elif overlay in ("0", "off", "false"):
            cfg.shell.overlay_mode = "none"

    if "VSH_VOICE_HANDLER" in os.environ:
        cfg.shell.voice_handler = os.environ["VSH_VOICE_HANDLER"]

    if "VSH_RESPONSE_BRIDGE" in os.environ:
        cfg.shell.response_bridge = os.environ["VSH_RESPONSE_BRIDGE"]

    if not config_path.exists():
        interactive_setup()
        return load_config()

    return cfg
