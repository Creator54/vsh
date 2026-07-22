from vsh.core.config import VshConfig
from vsh.providers.gcp_stt import GcpSTTProvider
from vsh.providers.http_audio import HttpSTTProvider, HttpTTSProvider
from vsh.providers.polly import AwsPollyTTSProvider
from vsh.providers.sarvam import SarvamSTTProvider, SarvamTTSProvider
from vsh.providers.supertonic import SupertonicTTSProvider
from vsh.providers.vosk import VoskSTTProvider

_STT_REGISTRY = {
    "custom_http": lambda c: HttpSTTProvider(c.stt),
    "vosk": lambda c: VoskSTTProvider(model_name=c.stt.model, model_url=c.stt.url),
    "gcp": lambda c: GcpSTTProvider(language_code=getattr(c.stt, "model", "en-US") or "en-US"),
    "sarvam": lambda c: SarvamSTTProvider(c.stt),
}

_TTS_REGISTRY = {
    "custom_http": lambda c: HttpTTSProvider(c.tts),
    "supertonic": lambda c: SupertonicTTSProvider(),
    "polly": lambda c: AwsPollyTTSProvider(voice=getattr(c.tts, "model", "Matthew") or "Matthew"),
    "sarvam": lambda c: SarvamTTSProvider(c.tts),
}


def resolve_stt(config: VshConfig):
    factory = _STT_REGISTRY.get(config.stt.provider)
    return factory(config) if factory else None


def resolve_tts(config: VshConfig):
    if config.tts.provider in ("", "none"):
        return None
    factory = _TTS_REGISTRY.get(config.tts.provider)
    return factory(config) if factory else None


def resolve_thinker(name: str, config: VshConfig):
    """Resolve a thinker by name with three-tier fallback.

    1. Built-in registry (echo, ollama)
    2. Config profiles ([llm.<name>])
    3. Raw CLI command fallback
    """
    # 1. Built-in registry
    if name == "echo":
        from vsh.providers.cli import CliThinker

        return CliThinker(command="echo You said: {}")
    elif name == "ollama":
        from vsh.providers.http import HttpThinker

        return HttpThinker(
            endpoint="http://localhost:11434/api/generate", format="ollama", model=config.llm.model or "llama3"
        )

    # 2. Config profiles
    if name in config.custom_thinkers:
        profile = config.custom_thinkers[name]
        thinker_type = profile.get("type", "cli")
        if thinker_type == "http":
            from vsh.providers.http import HttpThinker

            return HttpThinker(**profile)
        if thinker_type == "cli":
            from vsh.providers.cli import CliThinker

            return CliThinker(**profile)
        raise ValueError(f"Unknown thinker type '{thinker_type}' for profile '{name}'")

    # 3. Fallback: raw CLI command
    from vsh.providers.cli import CliThinker

    return CliThinker(command=name)
