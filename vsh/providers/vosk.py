from vsh.core.provider import STTProvider
from typing import Iterator
from loguru import logger
from pathlib import Path
import json
import os
import zipfile
import urllib.request
import ssl
from vosk import Model, KaldiRecognizer

class VoskSTTProvider(STTProvider):
    """Vosk Offline Speech-to-Text provider."""
    
    MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
    MODEL_NAME = "vosk-model-small-en-us-0.15"

    def __init__(self, model_path: str = None):
        if model_path is None:
            # ponytail: keep models in a consistent relative path
            model_path = str(Path(__file__).parent.parent.parent / "models" / self.MODEL_NAME)
        
        self._ensure_model(model_path)
        self.model = Model(model_path)
        self.sample_rate = 16000
        self.recognizer = KaldiRecognizer(self.model, self.sample_rate)

    def _ensure_model(self, model_path: str):
        if not os.path.exists(model_path):
            os.makedirs(os.path.dirname(model_path), exist_ok=True)
            logger.info(f"Downloading model {self.MODEL_NAME}...")
            zip_path = model_path + ".zip"
            
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(self.MODEL_URL, context=context) as response, open(zip_path, 'wb') as out_file:
                out_file.write(response.read())
            
            logger.info(f"Extracting model...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(os.path.dirname(model_path))
            os.remove(zip_path)
            logger.success(f"Model ready.")

    def transcribe_stream(self, audio_stream: Iterator[bytes]) -> str:
        results = []
        for chunk in audio_stream:
            if self.recognizer.AcceptWaveform(chunk):
                results.append(json.loads(self.recognizer.Result()).get("text", ""))
        
        results.append(json.loads(self.recognizer.FinalResult()).get("text", ""))
        return " ".join(filter(None, results))

    def transcribe_file(self, file_path: str) -> str:
        def file_gen():
            with open(file_path, "rb") as f:
                while True:
                    data = f.read(4000)
                    if not data: break
                    yield data
        return self.transcribe_stream(file_gen())
