import unittest
from vsh.main import LocalSpeech, VoskAdapter, SupertonicAdapter, AudioSignal

class TestProviders(unittest.TestCase):
    def test_engine_init(self):
        engine = LocalSpeech(VoskAdapter(), SupertonicAdapter())
        self.assertIsNotNone(engine.stt)
        self.assertIsNotNone(engine.tts)

    def test_supertonic_tts_synthesis(self):
        tts = SupertonicAdapter()
        # synthesize now returns a generator of AudioSignal objects
        sigs = list(tts.synthesize(text="test. check."))
        self.assertTrue(len(sigs) > 0)
        self.assertIsInstance(sigs[0], AudioSignal)
        self.assertEqual(sigs[0].rate, 44100)

if __name__ == "__main__":
    unittest.main()
