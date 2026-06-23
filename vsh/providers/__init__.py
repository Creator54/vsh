from vsh.core.config import VshConfig
from vsh.providers.cli import CliThinker
from vsh.providers.http import HttpThinker
from vsh.providers.http_audio import HttpSTTProvider, HttpTTSProvider
from vsh.providers.ollama import OllamaThinker
from vsh.providers.supertonic import SupertonicTTSProvider
from vsh.providers.thinker import EchoThinker
from vsh.providers.vosk import VoskSTTProvider

STT_PROVIDERS = {
    "vosk": VoskSTTProvider,
    "custom_http": HttpSTTProvider,
}

TTS_PROVIDERS = {
    "supertonic": SupertonicTTSProvider,
    "custom_http": HttpTTSProvider,
}


def resolve_stt(config: VshConfig):
    provider_name = config.stt.provider
    if provider_name == "custom_http":
        return HttpSTTProvider(config.stt)
    elif provider_name == "vosk":
        return VoskSTTProvider(model_name=config.stt.model, model_url=config.stt.url)
    elif provider_name in STT_PROVIDERS:
        return STT_PROVIDERS[provider_name]()
    return None


def resolve_tts(config: VshConfig):
    provider_name = config.tts.provider
    if provider_name == "custom_http":
        return HttpTTSProvider(config.tts)
    elif provider_name in TTS_PROVIDERS:
        return TTS_PROVIDERS[provider_name]()
    return None


THINKER_PROVIDERS = {
    "echo": EchoThinker,
    "ollama": OllamaThinker,
}


def resolve_thinker(name: str, config: VshConfig):
    """Resolve a thinker by name with three-tier fallback.

    1. Built-in registry (echo, ollama)
    2. Config profiles ([llm.<name>])
    3. Raw CLI command fallback
    """
    # 1. Built-in registry
    if name in THINKER_PROVIDERS:
        kwargs = {}
        if name == "ollama" and config.llm.model:
            kwargs["model"] = config.llm.model
        return THINKER_PROVIDERS[name](**kwargs)

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
