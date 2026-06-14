from vsh.providers.vosk import VoskSTTProvider
from vsh.providers.supertonic import SupertonicTTSProvider
from vsh.providers.thinker import EchoThinker
from vsh.providers.ollama import OllamaThinker

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

