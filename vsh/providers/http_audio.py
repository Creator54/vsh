import io
import wave
from collections.abc import Iterator

import numpy as np
from loguru import logger

from vsh.core.config import ProviderConfig


class HttpSTTProvider:
    """HTTP-based Speech-to-Text Provider (Whisper, Gemini, Sarvam, etc.)."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        if not self.config.endpoint:
            raise ValueError("HTTP STT endpoint is not configured")

        # Load API key directly from config (resolved from api_key_env by load_config)
        self.api_key = self.config.api_key
        self.format = getattr(self.config, "format", "openai_whisper")
        self.model = getattr(self.config, "model", "whisper-1")

    def transcribe_stream(self, audio_stream: Iterator[bytes], on_phrase=None, rate: int = 16000) -> str:
        # Collect raw PCM bytes
        pcm_bytes = b"".join(audio_stream)
        if not pcm_bytes:
            return ""

        # Convert to WAV
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm_bytes)
        wav_data = wav_io.getvalue()

        logger.debug(f"Sending {len(wav_data)} bytes of WAV audio to {self.config.endpoint} using format {self.format}")

        import requests

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            if self.format in ("openai_whisper", "sarvam"):
                files = {"file": ("audio.wav", wav_data, "audio/wav")}
                data = {"model": self.model} if self.model else {}
                response = requests.post(self.config.endpoint, headers=headers, files=files, data=data)
            else:
                # Fallback for base64 JSON APIs (e.g. Gemini)
                import base64

                headers["Content-Type"] = "application/json"
                payload = {"audio": base64.b64encode(wav_data).decode("utf-8"), "model": self.model}
                response = requests.post(self.config.endpoint, headers=headers, json=payload)

            response.raise_for_status()
            resp_json = response.json()

            # Extract text based on standard schema
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
    """HTTP-based Text-to-Speech Provider."""

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

            # Special case for ElevenLabs
            if self.format == "elevenlabs":
                headers["xi-api-key"] = self.api_key
                # Delete standard auth to avoid confusing the API
                if "Authorization" in headers:
                    del headers["Authorization"]

        payload = {
            "input": text,
            "text": text,  # Some APIs use 'text'
            "model": self.model,
            "response_format": "wav",
        }

        import requests

        try:
            response = requests.post(self.config.endpoint, headers=headers, json=payload)
            response.raise_for_status()
            audio_bytes = response.content

            # Parse the WAV header to extract PCM data
            with io.BytesIO(audio_bytes) as wav_io:
                try:
                    with wave.open(wav_io, "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                        # Convert to float32 numpy array normalized to [-1.0, 1.0]
                        audio_data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                        return audio_data
                except wave.Error:
                    # Fallback if raw PCM is returned instead of WAV
                    logger.warning("Failed to parse WAV header, assuming raw 16-bit PCM")
                    return np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP TTS request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            # Return empty array on failure
            return np.zeros(1, dtype=np.float32)
