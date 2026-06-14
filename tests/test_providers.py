import unittest
import numpy as np
from vsh.providers import STT_PROVIDERS, TTS_PROVIDERS

class TestProviders(unittest.TestCase):
    def test_registry(self):
        self.assertIn("vosk", STT_PROVIDERS)
        self.assertIn("supertonic", TTS_PROVIDERS)

    def test_supertonic_tts_init(self):
        provider = TTS_PROVIDERS["supertonic"]()
        # Just test that it can synthesize without crashing (mock-like text)
        audio = provider.synthesize("test")
        self.assertIsInstance(audio, np.ndarray)
        self.assertTrue(len(audio) > 0)

if __name__ == "__main__":
    unittest.main()
