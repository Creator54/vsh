import numpy as np
from loguru import logger


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
                OutputFormat="pcm",  # Raw PCM audio
                VoiceId=self.voice,
                Engine="neural",  # Use neural for highest quality
                SampleRate="16000",  # Safest standard rate
            )

            if "AudioStream" in response:
                audio_bytes = response["AudioStream"].read()
                # PCM is returned as 16-bit signed integers.
                audio_data = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

                # vsh hardcodes playback to 44100Hz. We must upsample 16000 -> 44100
                # using numpy linear interpolation.
                original_rate = 16000
                target_rate = 44100
                duration = len(audio_data) / original_rate
                target_length = int(duration * target_rate)

                x_old = np.linspace(0, duration, len(audio_data))
                x_new = np.linspace(0, duration, target_length)
                resampled_audio = np.interp(x_new, x_old, audio_data).astype(np.float32)

                return resampled_audio
            else:
                logger.error("No AudioStream in Polly response")
                return np.zeros(1, dtype=np.float32)

        except Exception as e:
            logger.error(f"AWS Polly TTS request failed: {e}")
            return np.zeros(1, dtype=np.float32)
