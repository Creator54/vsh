import warnings
# ponytail: silence deprecation noise at source
warnings.filterwarnings("ignore", category=DeprecationWarning)
import audioop
from vsh.core.provider import STTProvider
from typing import Iterator
from loguru import logger
from pathlib import Path
import json
import os
import shutil
import urllib.request
import ssl
from vosk import Model, KaldiRecognizer

class VoskSTTProvider(STTProvider):
    """Vosk Offline Speech-to-Text provider."""
    
    MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-en-in-0.5.zip"
    MODEL_NAME = "vosk-model-en-in-0.5"

    def __init__(self, model_name: str = None):
        self.model_name = model_name or self.MODEL_NAME
        # ponytail: keep models in a consistent relative path
        model_path = str(Path(__file__).parent.parent.parent / "models" / self.model_name)
        
        self._ensure_model(model_path)
        self.model = Model(model_path)
        self.sample_rate = 16000
        self.recognizer = KaldiRecognizer(self.model, self.sample_rate)

    def _ensure_model(self, model_path: str):
        if not os.path.exists(model_path):
            os.makedirs(os.path.dirname(model_path), exist_ok=True)
            logger.info(f"Downloading model {self.MODEL_NAME}...")
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(self.MODEL_URL, context=ctx) as r, open(model_path + ".zip", 'wb') as f:
                shutil.copyfileobj(r, f)
            logger.info("Extracting...")
            shutil.unpack_archive(model_path + ".zip", os.path.dirname(model_path))
            os.remove(model_path + ".zip")
            logger.success("Model ready.")

    def transcribe_stream(self, audio_stream: Iterator[bytes], on_phrase=None, rate: int = 16000) -> str:
        rec, res, st = KaldiRecognizer(self.model, 16000), [], None
        chunk_count = 0
        for chunk in audio_stream:
            chunk_count += 1
            if rate != 16000: chunk, st = audioop.ratecv(chunk, 2, 1, rate, 16000, st)
            if rec.AcceptWaveform(chunk):
                t = json.loads(rec.Result()).get("text", "")
                if t:
                    logger.debug(f"Vosk result: {t}")
                    res.append(t)
                    if on_phrase: on_phrase(t)
            else:
                p = json.loads(rec.PartialResult()).get("partial", "")
                if p: sys.stderr.write(f"\r\033[K• {p}"); sys.stderr.flush()
        
        logger.debug(f"Vosk stream finished. Total chunks: {chunk_count}")
        f = json.loads(rec.FinalResult()).get("text", "")
        if f:
            logger.debug(f"Vosk final: {f}")
            res.append(f)
            if on_phrase: on_phrase(f)
        return " ".join(filter(None, res))

    def transcribe_file(self, file_path: str) -> str:
        with open(file_path, "rb") as f:
            return self.transcribe_stream(iter(lambda: f.read(4000), b""))
