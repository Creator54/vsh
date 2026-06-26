import base64
import io
import logging
import wave

import numpy as np
import requests

logger = logging.getLogger(__name__)


class SarvamSTTProvider:
    """Sarvam AI STT Provider (Translates Indian languages to English)."""

    def __init__(self, config):
        self.config = config
        self.api_key = self.config.api_key
        if not self.api_key:
            raise ValueError("Sarvam API key is required")

        self.endpoint = "https://api.sarvam.ai/speech-to-text-translate"

    def transcribe_stream(self, audio_stream, on_phrase=None) -> str:
        with io.BytesIO() as wav_io:
            with wave.open(wav_io, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                for chunk in audio_stream:
                    wf.writeframes(chunk)
            wav_data = wav_io.getvalue()

        # Sarvam requires api-subscription-key
        headers = {"api-subscription-key": self.api_key}
        files = {"file": ("audio.wav", wav_data, "audio/wav")}

        # Required parameter for speech-to-text-translate
        data = {"prompt": "Terminal command voice input."}

        try:
            response = requests.post(self.endpoint, headers=headers, files=files, data=data)
            response.raise_for_status()
            resp_json = response.json()

            # Sarvam might return {"transcript": "text"} or {"text": "text"}
            text = resp_json.get("transcript") or resp_json.get("text", "")
            if text and on_phrase:
                on_phrase(text)
            return text
        except Exception as e:
            logger.error(f"Sarvam STT failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return ""


class SarvamTTSProvider:
    """Sarvam AI TTS Provider."""

    def __init__(self, config):
        self.config = config
        self.api_key = self.config.api_key
        if not self.api_key:
            raise ValueError("Sarvam API key is required")
        self.endpoint = "https://api.sarvam.ai/text-to-speech"

    def synthesize(self, text: str) -> np.ndarray:
        headers = {"api-subscription-key": self.api_key, "Content-Type": "application/json"}

        payload = {
            "inputs": [text],
            "target_language_code": "hi-IN",
            "speaker": getattr(self.config, "model", "priya") or "priya",
            "model": "bulbul:v3",
            "speech_sample_rate": 16000,
            "enable_preprocessing": True,
        }

        try:
            response = requests.post(self.endpoint, headers=headers, json=payload)
            response.raise_for_status()
            resp_json = response.json()

            audios = resp_json.get("audios", [])
            if not audios:
                raise ValueError("No audio returned from Sarvam")

            audio_bytes = base64.b64decode(audios[0])

            with io.BytesIO(audio_bytes) as wav_io:
                with wave.open(wav_io, "rb") as wf:
                    frames = wf.readframes(wf.getnframes())
                    audio_data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                    return audio_data

        except Exception as e:
            logger.error(f"Sarvam TTS failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise
