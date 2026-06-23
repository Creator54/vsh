import numpy as np
from supertonic import TTS


class SupertonicTTSProvider:
    """Real Supertonic Text-to-Speech provider."""

    def __init__(self, voice="F1"):
        self.engine = TTS(auto_download=True)
        self.voice_style = self.engine.get_voice_style(voice_name=voice)

    def synthesize(self, text: str) -> np.ndarray:
        # ponytail: 8 steps is a good balance of quality and speed
        wav, _ = self.engine.synthesize(text=text, voice_style=self.voice_style, total_steps=8, speed=1.0, lang="en")
        return wav.flatten()
