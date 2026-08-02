import base64
import tempfile
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

import numpy as np

from vsh.core.config import ProviderConfig, VshConfig
from vsh.providers import resolve_tts
from vsh.providers.audio_format import decode_pcm16_wav, encode_pcm_wav
from vsh.providers.http_audio import HttpSTTProvider, HttpTTSProvider
from vsh.providers.polly import AwsPollyTTSProvider
from vsh.providers.sarvam import SarvamTTSProvider
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
        self.assertEqual(post.call_args.kwargs["timeout"], 15)

    @patch("requests.post")
    def test_http_tts_decodes_wav_samples(self, post):
        pcm = np.array([-32768, 0, 32767], dtype=np.int16)
        response = MagicMock(content=encode_pcm_wav(pcm.tobytes(), 24000))
        post.return_value = response
        provider = HttpTTSProvider(ProviderConfig(endpoint="https://speech.example", format="openai_tts"))

        result = provider.synthesize("hello")

        np.testing.assert_array_equal(result, pcm.astype(np.float32) / 32768.0)
        self.assertEqual(provider.sample_rate, 24000)
        self.assertEqual(post.call_args.kwargs["timeout"], 15)

    def test_polly_uses_native_pcm_rate(self):
        pcm = np.array([-1000, 0, 1000], dtype=np.int16)
        provider = AwsPollyTTSProvider.__new__(AwsPollyTTSProvider)
        provider.voice = "Matthew"
        provider.sample_rate = 16000
        provider.client = MagicMock()
        provider.client.synthesize_speech.return_value = {"AudioStream": BytesIO(pcm.tobytes())}

        result = provider.synthesize("hello")

        np.testing.assert_array_equal(result, pcm.astype(np.float32) / 32768.0)
        self.assertEqual(provider.sample_rate, 16000)

    @patch("requests.post")
    def test_sarvam_uses_native_wav_rate(self, post):
        pcm = np.array([-1000, 0, 1000], dtype=np.int16)
        response = MagicMock()
        response.json.return_value = {"audios": [base64.b64encode(encode_pcm_wav(pcm.tobytes(), 24000)).decode()]}
        post.return_value = response
        provider = SarvamTTSProvider(ProviderConfig(api_key="secret", model="priya"))

        result = provider.synthesize("hello")

        np.testing.assert_array_equal(result, pcm.astype(np.float32) / 32768.0)
        self.assertEqual(provider.sample_rate, 24000)
        self.assertEqual(post.call_args.kwargs["json"]["speech_sample_rate"], 24000)

    def test_vosk_provider_requires_configured_model_name(self):
        from vsh.providers.vosk import VoskSTTProvider

        with self.assertRaises(ValueError) as ctx:
            VoskSTTProvider(model_name="", model_url="")
        self.assertIn("No Vosk model name configured", str(ctx.exception))

    def test_vosk_provider_requires_url_when_model_path_missing(self):
        from vsh.providers.vosk import VoskSTTProvider

        with (
            patch("os.path.exists", return_value=False),
            self.assertRaises(ValueError) as ctx,
        ):
            VoskSTTProvider(model_name="nonexistent-model", model_url="")
        self.assertIn("no download URL is configured", str(ctx.exception))

    @patch("vsh.providers.vosk.Model")
    def test_vosk_provider_loads_existing_local_model(self, mock_model_cls):
        from vsh.providers.vosk import VoskSTTProvider

        with patch("os.path.exists", return_value=True):
            provider = VoskSTTProvider(model_name="installed-model")
            self.assertEqual(provider.model_name, "installed-model")
            mock_model_cls.assert_called_once()

    def test_vosk_download_uses_verified_tls(self):
        from vsh.providers.vosk import VoskSTTProvider

        response = MagicMock()
        response.headers = {}
        response.read.return_value = b""
        response.__enter__.return_value = response
        provider = VoskSTTProvider.__new__(VoskSTTProvider)
        provider.model_name = "model"

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("urllib.request.urlopen", return_value=response) as urlopen,
                patch("shutil.unpack_archive"),
            ):
                provider._ensure_model(f"{temp_dir}/model", "https://example.test/model.zip")

        self.assertNotIn("context", urlopen.call_args.kwargs)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
