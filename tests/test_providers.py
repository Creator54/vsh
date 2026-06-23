import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from vsh.providers.supertonic import SupertonicTTSProvider


class TestProviders(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
