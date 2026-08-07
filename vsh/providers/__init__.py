from loguru import logger

from vsh.core.config import VshConfig
def resolve_stt(config: VshConfig):
    provider = config.stt.provider
    if not provider:
        return None
    try:
        if provider == "custom_http":
            from vsh.providers.http_audio import HttpSTTProvider

            return HttpSTTProvider(config.stt)
        elif provider == "vosk":
            from vsh.providers.vosk import VoskSTTProvider

            return VoskSTTProvider(model_name=config.stt.model, model_url=config.stt.url)
        elif provider == "gcp":
            from vsh.providers.gcp_stt import GcpSTTProvider

            return GcpSTTProvider(language_code=getattr(config.stt, "model", "en-US") or "en-US")
        elif provider == "sarvam":
            from vsh.providers.sarvam import SarvamSTTProvider

            return SarvamSTTProvider(config.stt)
        return None
    except Exception as e:
        logger.error(f"Failed to initialize STT provider '{config.stt.provider}': {e}")
        return None


def resolve_tts(config: VshConfig):
    if config.tts.provider in ("", "none"):
        return None
    provider = config.tts.provider
    try:
        if provider == "custom_http":
            from vsh.providers.http_audio import HttpTTSProvider

            return HttpTTSProvider(config.tts)
        elif provider == "supertonic":
            from vsh.providers.supertonic import SupertonicTTSProvider

            return SupertonicTTSProvider()
        elif provider == "polly":
            from vsh.providers.polly import AwsPollyTTSProvider

            return AwsPollyTTSProvider(voice=getattr(config.tts, "model", "Matthew") or "Matthew")
        elif provider == "sarvam":
            from vsh.providers.sarvam import SarvamTTSProvider

            return SarvamTTSProvider(config.tts)
        return None
    except Exception as e:
        logger.error(f"Failed to initialize TTS provider '{config.tts.provider}': {e}")
        return None


def resolve_thinker(name: str, config: VshConfig):
    """Find a built-in, configured, or command-line AI provider."""
    if name == "echo":
        from vsh.providers.cli import CliThinker

        return CliThinker(command="echo You said: {}")
    elif name == "ollama":
        from vsh.providers.http import HttpThinker

        return HttpThinker(
            endpoint="http://localhost:11434/api/generate", format="ollama", model=config.llm.model or "llama3"
        )

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

    from vsh.providers.cli import CliThinker

    return CliThinker(command=name)
