import os
from pathlib import Path
from dataclasses import dataclass, field
import tomllib
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
    device_index: int = None

@dataclass
class VshConfig:
    shell: ShellConfig = field(default_factory=ShellConfig)
    keybinds: KeybindConfig = field(default_factory=KeybindConfig)
    stt: ProviderConfig = field(default_factory=lambda: ProviderConfig("vosk"))
    tts: ProviderConfig = field(default_factory=lambda: ProviderConfig("supertonic"))
    llm: ProviderConfig = field(default_factory=ProviderConfig)

def _get_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vsh" / "config.toml"

def load_config() -> VshConfig:
    """Load configuration from file, then apply environment variable overrides."""
    config_path = _get_config_path()
    cfg = VshConfig()
    
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            
            for k, v in data.items():
                if hasattr(cfg, k): vars(getattr(cfg, k)).update(v)
                
        except Exception as e:
            logger.error(f"Failed to load {config_path}: {e}")

    # Environment overrides
    if "VSH_SHELL" in os.environ:
        cfg.shell.inner_shell = os.environ["VSH_SHELL"]
        
    if "VSH_VOICE" in os.environ:
        val = os.environ["VSH_VOICE"].lower()
        cfg.shell.voice_on_start = val in ("1", "true", "yes", "on")

    return cfg
