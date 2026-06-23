from vsh.core.config import VshConfig


def resolve_stt(config: VshConfig):
    provider_name = config.stt.provider
    if provider_name == "custom_http":
        from vsh.providers.http_audio import HttpSTTProvider

        return HttpSTTProvider(config.stt)
    elif provider_name == "vosk":
        from vsh.providers.vosk import VoskSTTProvider

        return VoskSTTProvider(model_name=config.stt.model, model_url=config.stt.url)
    return None


def resolve_tts(config: VshConfig):
    provider_name = config.tts.provider
    if provider_name == "custom_http":
        from vsh.providers.http_audio import HttpTTSProvider

        return HttpTTSProvider(config.tts)
    elif provider_name == "supertonic":
        from vsh.providers.supertonic import SupertonicTTSProvider

        return SupertonicTTSProvider()
    return None


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
