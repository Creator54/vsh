import threading

import numpy as np


class SupertonicTTSProvider:
    """Real Supertonic Text-to-Speech provider."""

    def __init__(self, voice="F1"):
        self.voice = voice
        self.engine = None
        self.voice_style = None
        self._lock = threading.Lock()

        # Load model in background
        threading.Thread(target=self._bg_load, daemon=True).start()

    def _bg_load(self):
        # We import TTS here so the import itself doesn't block
        from supertonic import TTS

        with self._lock:
            engine = TTS(auto_download=True)
            self.voice_style = engine.get_voice_style(voice_name=self.voice)
            self.engine = engine

    def synthesize(self, text: str) -> np.ndarray:
        # Block until engine is loaded
        with self._lock:
            engine = self.engine
            voice_style = self.voice_style

        if not engine:
            # Fallback if background load failed (though it shouldn't unless error)
            from supertonic import TTS

            engine = TTS(auto_download=True)
            voice_style = engine.get_voice_style(voice_name=self.voice)

        # Eight steps balance quality and speed.
        wav, _ = engine.synthesize(text=text, voice_style=voice_style, total_steps=8, speed=1.0, lang="en")
        return wav.flatten()
