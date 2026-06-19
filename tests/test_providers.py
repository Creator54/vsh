import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from vsh.main import LocalSpeech
from vsh.providers.supertonic import SupertonicTTSProvider
from vsh.providers.vosk import VoskSTTProvider


class TestProviders(unittest.TestCase):
    @patch("vsh.providers.vosk.Model")
    @patch("vsh.providers.vosk.KaldiRecognizer")
    def test_engine_init(self, mock_recognizer, mock_model):
        stt = VoskSTTProvider()
        tts = MagicMock()
        engine = LocalSpeech(stt, tts)
        self.assertIsNotNone(engine.stt)
        self.assertIsNotNone(engine.tts)

    @patch("vsh.providers.supertonic.TTS")
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


if __name__ == "__main__":
    unittest.main()
