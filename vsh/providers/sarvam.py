import base64
import logging

import numpy as np
import requests

from vsh.providers.audio_format import decode_pcm16_wav, encode_pcm_wav, resample

logger = logging.getLogger(__name__)


class SarvamSTTProvider:
    """Translate spoken Indian languages to English with Sarvam AI."""

    def __init__(self, config):
        self.config = config
        self.api_key = self.config.api_key
        if not self.api_key:
            raise ValueError("Sarvam API key is required")

        self.endpoint = "https://api.sarvam.ai/speech-to-text-translate"

    def transcribe_stream(self, audio_stream, on_phrase=None) -> str:
        wav_data = encode_pcm_wav(b"".join(audio_stream), 16000)

        headers = {"api-subscription-key": self.api_key}
        files = {"file": ("audio.wav", wav_data, "audio/wav")}
        data = {"prompt": "Terminal command voice input."}

        try:
            response = requests.post(self.endpoint, headers=headers, files=files, data=data)
            response.raise_for_status()
            resp_json = response.json()

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
    """Generate speech with Sarvam AI."""

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
            return resample(decode_pcm16_wav(audio_bytes), 16000, 44100)

        except Exception as e:
            logger.error(f"Sarvam TTS failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise
