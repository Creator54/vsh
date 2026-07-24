import sys
from collections.abc import Iterator

from loguru import logger


class GcpSTTProvider:
    """Google Cloud Speech-to-Text provider."""

    def __init__(self, language_code="en-US"):
        try:
            from google.cloud import speech

            self.speech = speech
            self.client = speech.SpeechClient()
        except ImportError as e:
            raise ImportError("google-cloud-speech is not installed. Please install it to use GCP STT.") from e
        except Exception as e:
            logger.error(f"Failed to initialize GCP Speech client (check GOOGLE_APPLICATION_CREDENTIALS): {e}")
            raise

        self.language_code = language_code

    def transcribe_stream(self, audio_stream: Iterator[bytes], on_phrase=None, rate: int = 16000) -> str:
        def request_generator():
            for chunk in audio_stream:
                yield self.speech.StreamingRecognizeRequest(audio_content=chunk)

        config = self.speech.RecognitionConfig(
            encoding=self.speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=rate,
            language_code=self.language_code,
        )

        streaming_config = self.speech.StreamingRecognitionConfig(config=config, interim_results=True)

        try:
            responses = self.client.streaming_recognize(
                config=streaming_config,
                requests=request_generator(),
            )
        except Exception as e:
            logger.error(f"GCP STT streaming request failed to start: {e}")
            return ""

        final_transcripts = []
        try:
            for response in responses:
                if not response.results:
                    continue

                result = response.results[0]
                if not result.alternatives:
                    continue

                transcript = result.alternatives[0].transcript

                if result.is_final:
                    logger.debug(f"GCP STT final: {transcript}")
                    final_transcripts.append(transcript.strip())
                    if on_phrase:
                        on_phrase(transcript.strip())
                    sys.stderr.write("\r\033[K")
                    sys.stderr.flush()
                else:
                    sys.stderr.write(f"\r\033[K• {transcript.strip()}")
                    sys.stderr.flush()

        except Exception as e:
            logger.error(f"Error during GCP STT streaming: {e}")
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

        return " ".join(final_transcripts)
