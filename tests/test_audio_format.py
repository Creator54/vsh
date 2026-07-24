import io
import wave

import numpy as np

from vsh.providers.audio_format import decode_pcm16, decode_pcm16_wav, encode_pcm_wav, resample


def test_pcm_wav_round_trip_is_sample_exact():
    samples = np.array([-32768, -123, 0, 456, 32767], dtype=np.int16)

    encoded = encode_pcm_wav(samples.tobytes(), 16000)

    with wave.open(io.BytesIO(encoded), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 16000
    np.testing.assert_array_equal(decode_pcm16_wav(encoded), decode_pcm16(samples.tobytes()))


def test_resample_matches_the_existing_linear_interpolation():
    audio = np.array([-1.0, -0.25, 0.5, 1.0], dtype=np.float32)
    duration = len(audio) / 16000
    expected = np.interp(
        np.linspace(0, duration, int(duration * 44100)),
        np.linspace(0, duration, len(audio)),
        audio,
    ).astype(np.float32)

    np.testing.assert_array_equal(resample(audio, 16000, 44100), expected)
