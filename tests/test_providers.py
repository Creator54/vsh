import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

import numpy as np

from vsh.core.config import ProviderConfig, VshConfig
from vsh.providers import resolve_tts
from vsh.providers.audio_format import decode_pcm16_wav, encode_pcm_wav, resample
from vsh.providers.http_audio import HttpSTTProvider, HttpTTSProvider
from vsh.providers.polly import AwsPollyTTSProvider
from vsh.providers.supertonic import SupertonicTTSProvider


class TestProviders(unittest.TestCase):
    def test_none_disables_tts(self):
        config = VshConfig()
        config.tts.provider = "none"

        self.assertIsNone(resolve_tts(config))

    @patch("supertonic.TTS")
    def test_supertonic_tts_synthesis(self, mock_tts_class):
        mock_engine = MagicMock()
        mock_engine.get_voice_style.return_value = MagicMock()
        mock_wav = MagicMock()
        mock_wav.flatten.return_value = np.zeros(1000)
        mock_engine.synthesize.return_value = (mock_wav, 1.0)
        mock_tts_class.return_value = mock_engine

        tts = SupertonicTTSProvider()
        result = tts.synthesize(text="test. check.")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, np.ndarray)

    @patch("requests.post")
    def test_http_stt_sends_the_same_pcm_as_wav(self, post):
        response = MagicMock()
        response.json.return_value = {"text": "pwd"}
        post.return_value = response
        provider = HttpSTTProvider(
            ProviderConfig(endpoint="https://speech.example", format="openai_whisper", model="whisper")
        )
        pcm = np.array([-1000, 0, 1000], dtype=np.int16).tobytes()

        result = provider.transcribe_stream([pcm[:2], pcm[2:]], rate=16000)

        self.assertEqual(result, "pwd")
        wav = post.call_args.kwargs["files"]["file"][1]
        np.testing.assert_array_equal(decode_pcm16_wav(wav), np.frombuffer(pcm, dtype=np.int16) / 32768.0)

    @patch("requests.post")
    def test_http_tts_decodes_wav_samples(self, post):
        pcm = np.array([-32768, 0, 32767], dtype=np.int16)
        response = MagicMock(content=encode_pcm_wav(pcm.tobytes(), 44100))
        post.return_value = response
        provider = HttpTTSProvider(ProviderConfig(endpoint="https://speech.example", format="openai_tts"))

        result = provider.synthesize("hello")

        np.testing.assert_array_equal(result, pcm.astype(np.float32) / 32768.0)

    def test_polly_keeps_existing_pcm_resampling(self):
        pcm = np.array([-1000, 0, 1000], dtype=np.int16)
        provider = AwsPollyTTSProvider.__new__(AwsPollyTTSProvider)
        provider.voice = "Matthew"
        provider.client = MagicMock()
        provider.client.synthesize_speech.return_value = {"AudioStream": BytesIO(pcm.tobytes())}

        result = provider.synthesize("hello")

        expected = resample(pcm.astype(np.float32) / 32768.0, 16000, 44100)
        np.testing.assert_array_equal(result, expected)


if __name__ == "__main__":
    unittest.main()
