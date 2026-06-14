from vsh.providers.vosk import VoskSTTProvider
from vsh.providers.supertonic import SupertonicTTSProvider

STT_PROVIDERS = {
    "vosk": VoskSTTProvider,
}

TTS_PROVIDERS = {
    "supertonic": SupertonicTTSProvider,
}
