import wave
from collections.abc import Iterator

import numpy as np
from loguru import logger

from vsh.core.config import ProviderConfig
from vsh.providers.audio_format import decode_pcm16, decode_pcm16_wav, encode_pcm_wav


class HttpSTTProvider:
    """Send speech to an HTTP transcription provider."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        if not self.config.endpoint:
            raise ValueError("HTTP STT endpoint is not configured")

        self.api_key = self.config.api_key
        self.format = getattr(self.config, "format", "openai_whisper")
        self.model = getattr(self.config, "model", "whisper-1")

    def transcribe_stream(self, audio_stream: Iterator[bytes], on_phrase=None, rate: int = 16000) -> str:
        pcm_bytes = b"".join(audio_stream)
        if not pcm_bytes:
            return ""

        wav_data = encode_pcm_wav(pcm_bytes, rate)

        logger.debug(f"Sending {len(wav_data)} bytes of WAV audio to {self.config.endpoint} using format {self.format}")

        import requests

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            if self.format in ("openai_whisper", "sarvam"):
                files = {"file": ("audio.wav", wav_data, "audio/wav")}
                data = {"model": self.model} if self.model else {}
                if self.format == "openai_whisper":
                    data.update(
                        {
                            "temperature": "0.0",
                            "prompt": "Terminal voice command.",
                            "language": "en",
                        }
                    )
                response = requests.post(self.config.endpoint, headers=headers, files=files, data=data)
            else:
                import base64

                headers["Content-Type"] = "application/json"
                payload = {"audio": base64.b64encode(wav_data).decode("utf-8"), "model": self.model}
                response = requests.post(self.config.endpoint, headers=headers, json=payload)

            response.raise_for_status()
            resp_json = response.json()

            res = resp_json.get("text") or resp_json.get("transcript")
            if not res:
                logger.warning(f"Unexpected STT response format: {resp_json}")
                res = str(resp_json)

            if res and on_phrase:
                on_phrase(res)
            return res
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP STT request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return ""


class HttpTTSProvider:
    """Request speech from an HTTP provider."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        if not self.config.endpoint:
            raise ValueError("HTTP TTS endpoint is not configured")

        self.api_key = self.config.api_key
        self.format = getattr(self.config, "format", "openai_tts")
        self.model = getattr(self.config, "model", "tts-1")

    def synthesize(self, text: str) -> np.ndarray:
        logger.debug(f"Synthesizing text via HTTP TTS ({self.format}): {text}")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

            if self.format == "elevenlabs":
                headers["xi-api-key"] = self.api_key
                del headers["Authorization"]

        payload = {
            "input": text,
            "text": text,
            "model": self.model,
            "response_format": "wav",
        }

        import requests

        try:
            response = requests.post(self.config.endpoint, headers=headers, json=payload)
            response.raise_for_status()
            audio_bytes = response.content

            try:
                return decode_pcm16_wav(audio_bytes)
            except wave.Error:
                logger.warning("Failed to parse WAV header, assuming raw 16-bit PCM")
                return decode_pcm16(audio_bytes)
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP TTS request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return np.zeros(1, dtype=np.float32)
