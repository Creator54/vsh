import numpy as np
from loguru import logger

from vsh.providers.audio_format import decode_pcm16, resample


class AwsPollyTTSProvider:
    """AWS Polly Text-to-Speech provider."""

    def __init__(self, voice="Matthew"):
        try:
            import boto3

            self.client = boto3.client("polly")
        except ImportError as e:
            raise ImportError("boto3 is not installed. Please install it to use AWS Polly.") from e
        except Exception as e:
            logger.error(f"Failed to initialize AWS Polly client: {e}")
            raise

        self.voice = voice

    def synthesize(self, text: str) -> np.ndarray:
        logger.debug(f"Synthesizing text via AWS Polly ({self.voice}): {text}")
        try:
            response = self.client.synthesize_speech(
                Text=text,
                OutputFormat="pcm",
                VoiceId=self.voice,
                Engine="neural",
                SampleRate="16000",
            )

            if "AudioStream" in response:
                audio_bytes = response["AudioStream"].read()
                return resample(decode_pcm16(audio_bytes), 16000, 44100)
            logger.error("No AudioStream in Polly response")
            return np.zeros(1, dtype=np.float32)

        except Exception as e:
            logger.error(f"AWS Polly TTS request failed: {e}")
            return np.zeros(1, dtype=np.float32)
