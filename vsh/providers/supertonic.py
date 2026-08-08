import fcntl
import os
import threading

import numpy as np

from vsh.core.logger import logger


class SupertonicTTSProvider:
    """Supertonic text-to-speech provider."""

    def __init__(self, voice="F1"):
        self.voice = voice
        self.sample_rate = 44100
        self.engine = None
        self.voice_style = None
        self._lock = threading.Lock()

    def _load(self):
        from supertonic import TTS
        from supertonic.loader import download_model, get_cache_dir, has_all_onnx_modules

        model_dir = get_cache_dir()
        lock_path = model_dir.parent / f".{model_dir.name}.vsh.lock"
        with lock_path.open("a+") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.info("Waiting for another VSH session to install the Supertonic model...")
                fcntl.flock(lock, fcntl.LOCK_EX)
            if not has_all_onnx_modules(model_dir):
                download_model(model_dir)
        engine = TTS(auto_download=False)
        self.voice_style = engine.get_voice_style(voice_name=self.voice)
        self.sample_rate = engine.sample_rate
        self.engine = engine

    def synthesize(self, text: str) -> np.ndarray:
        with self._lock:
            if self.engine is None:
                self._load()

            # Eight steps balance quality and speed.
            wav, _ = self.engine.synthesize(
                text=text, voice_style=self.voice_style, total_steps=8, speed=1.0, lang="en"
            )
        return wav.flatten()
