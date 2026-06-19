from vsh.core.config import VshConfig
from vsh.core.provider import Thinker
from vsh.providers.cli import CliThinker
from vsh.providers.http import HttpThinker
from vsh.providers.ollama import OllamaThinker
from vsh.providers.supertonic import SupertonicTTSProvider
from vsh.providers.thinker import EchoThinker
from vsh.providers.vosk import VoskSTTProvider

STT_PROVIDERS = {
    "vosk": VoskSTTProvider,
}

TTS_PROVIDERS = {
    "supertonic": SupertonicTTSProvider,
}

THINKER_PROVIDERS = {
    "echo": EchoThinker,
    "ollama": OllamaThinker,
}


def resolve_thinker(name: str, config: VshConfig) -> Thinker:
    """Resolve a thinker by name with three-tier fallback.

    1. Built-in registry (echo, ollama)
    2. Config profiles ([llm.<name>])
    3. Raw CLI command fallback
    """
    # 1. Built-in registry
    if name in THINKER_PROVIDERS:
        return THINKER_PROVIDERS[name]()

    # 2. Config profiles
    if name in config.custom_thinkers:
        profile = config.custom_thinkers[name]
        thinker_type = profile.get("type", "cli")
        if thinker_type == "http":
            return HttpThinker(**profile)
        if thinker_type == "cli":
            return CliThinker(**profile)
        raise ValueError(f"Unknown thinker type '{thinker_type}' for profile '{name}'")

    # 3. Fallback: raw CLI command
    return CliThinker(command=name)
