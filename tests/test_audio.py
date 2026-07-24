import queue
import threading

import numpy as np

from vsh.core.audio import MicStream, detect_phrase


class SequenceVad:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def is_speech(self, frame, rate):
        assert len(frame) == rate * 20 // 1000 * 2
        return next(self.decisions)


def pcm_frame(amplitude: int, milliseconds: int = 20) -> bytes:
    samples = np.empty(16_000 * milliseconds // 1000, dtype=np.int16)
    samples[::2] = amplitude
    samples[1::2] = -amplitude
    return samples.tobytes()


def sine_frames(amplitude: int, frequency: int, count: int) -> np.ndarray:
    samples = np.arange(320 * count)
    return amplitude * np.sin(2 * np.pi * frequency * samples / 16_000)


def split_frames(signal: np.ndarray) -> list[bytes]:
    pcm = np.clip(signal, -32768, 32767).astype(np.int16)
    return [pcm[offset : offset + 320].tobytes() for offset in range(0, len(pcm), 320)]


def sine_pcm_frames(amplitude: int, frequency: int, count: int) -> list[bytes]:
    return split_frames(sine_frames(amplitude, frequency, count))


def test_silence_is_not_returned_for_transcription():
    frames = [pcm_frame(0) for _ in range(20)]

    capture = detect_phrase(frames, vad=SequenceVad([False] * len(frames)))

    assert not capture.accepted
    assert capture.chunks == ()
    assert capture.voiced_ms == 0


def test_vad_rejects_loud_background_noise():
    frames = [pcm_frame(6000) for _ in range(30)]

    capture = detect_phrase(frames, vad=SequenceVad([False] * len(frames)))

    assert not capture.accepted
    assert capture.chunks == ()


def test_real_vad_does_not_treat_steady_broadband_noise_as_a_phrase():
    random = np.random.default_rng(7)
    frames = [random.integers(-3000, 3001, 320, dtype=np.int16).tobytes() for _ in range(200)]

    capture = detect_phrase(frames)

    assert not capture.accepted


def test_quiet_high_frequency_speech_can_rise_above_low_frequency_fan_noise():
    fan = sine_frames(5000, 100, 53)
    voice = np.zeros_like(fan)
    voice[320 * 25 : 320 * 38] = sine_frames(10000, 1000, 13)
    signal = np.clip(fan + voice, -32768, 32767).astype(np.int16)
    frames = [signal[offset : offset + 320].tobytes() for offset in range(0, len(signal), 320)]

    capture = detect_phrase(frames, vad=SequenceVad([True] * len(frames)))

    assert capture.accepted


def test_vad_positive_fan_variation_is_not_returned_as_speech():
    decisions = [True] * 53
    amplitudes = [3000] * 25 + [4000] * 13 + [3000] * 15
    frames = [pcm_frame(amplitude) for amplitude in amplitudes]

    capture = detect_phrase(frames, vad=SequenceVad(decisions))

    assert not capture.accepted
    assert capture.chunks == ()


def test_transient_shorter_than_confirmed_speech_minimum_is_rejected():
    decisions = [False] * 29 + [True] * 12 + [False] * 15
    frames = [pcm_frame(100)] * 29 + sine_pcm_frames(5000, 1000, 12) + [pcm_frame(100)] * 15
    activity = []

    capture = detect_phrase(frames, vad=SequenceVad(decisions), activity_callback=activity.append)

    assert not capture.accepted
    assert capture.voiced_ms == 0
    assert activity == []


def test_frames_during_known_keyboard_activity_are_discarded():
    ignored = iter([True] * 8 + [False] * 40)
    decisions = [False] * 40
    frames = [pcm_frame(6000)] * 8 + [pcm_frame(100)] * 40

    capture = detect_phrase(
        frames,
        vad=SequenceVad(decisions),
        ignore_check=lambda: next(ignored),
    )

    assert not capture.accepted
    assert capture.chunks == ()


def test_short_command_keeps_only_preroll_and_phrase_audio():
    decisions = [False] * 30 + [True] * 13 + [False] * 15
    frames = (
        [pcm_frame(100 + index) for index in range(30)]
        + sine_pcm_frames(5000, 1000, 13)
        + [pcm_frame(100)] * 15
    )
    activity = []

    capture = detect_phrase(
        frames,
        vad=SequenceVad(decisions),
        activity_callback=activity.append,
    )

    assert capture.accepted
    assert capture.voiced_ms == 260
    assert len(capture.chunks) == 33
    assert capture.chunks[0] == pcm_frame(125)
    assert activity == [True, False]


def test_input_chunks_are_split_into_twenty_millisecond_vad_frames():
    decisions = [False] * 30 + [True] * 13 + [False] * 15
    payload = b"".join([pcm_frame(100)] * 30 + sine_pcm_frames(5000, 1000, 13) + [pcm_frame(100)] * 15)
    chunks = [payload[offset : offset + 2048] for offset in range(0, len(payload), 2048)]

    capture = detect_phrase(chunks, vad=SequenceVad(decisions))

    assert capture.accepted
    assert all(len(frame) == 640 for frame in capture.chunks)


def test_unvoiced_frames_set_the_adaptive_energy_floor():
    decisions = [False] * 25 + [True] * 6 + [False] * 15
    frames = [pcm_frame(2000)] * 25 + [pcm_frame(2100)] * 6 + [pcm_frame(2000)] * 15
    thresholds = []

    capture = detect_phrase(
        frames,
        vad=SequenceVad(decisions),
        threshold=1000,
        volume_callback=lambda _energy, threshold: thresholds.append(threshold),
    )

    assert not capture.accepted
    assert thresholds[-1] > 4000


def test_phrase_stops_at_the_maximum_duration():
    decisions = [False] * 25 + [True] * 20
    frames = [pcm_frame(100)] * 25 + sine_pcm_frames(5000, 1000, 20)

    capture = detect_phrase(
        frames,
        vad=SequenceVad(decisions),
        max_phrase_ms=400,
    )

    assert capture.accepted
    assert capture.reason == "max_phrase"
    assert len(capture.chunks) == 20


def test_post_calibration_road_noise_is_rejected_and_updates_the_floor():
    frame_count = 75
    samples = np.arange(320 * frame_count) / 16_000
    signal = np.zeros(320 * frame_count)
    signal[: 320 * 30] = 100 * np.sin(2 * np.pi * 100 * samples[: 320 * 30])
    signal[320 * 30 : 320 * 60] = 10_000 * (
        0.7 * np.sin(2 * np.pi * 90 * samples[320 * 30 : 320 * 60])
        + 0.3 * np.sin(2 * np.pi * 180 * samples[320 * 30 : 320 * 60])
    )
    signal[320 * 60 :] = 100 * np.sin(2 * np.pi * 100 * samples[320 * 60 :])
    decisions = [False] * 30 + [True] * 30 + [False] * 15
    thresholds = []

    capture = detect_phrase(
        split_frames(signal),
        vad=SequenceVad(decisions),
        volume_callback=lambda _energy, threshold: thresholds.append(threshold),
    )

    assert not capture.accepted
    assert thresholds[59] > thresholds[29]


def test_varying_road_noise_is_rejected_when_vad_sticks_true():
    frame_count = 500
    samples = np.arange(320 * frame_count) / 16_000
    envelope = 2500 + 5500 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * samples))
    signal = envelope * (
        0.75 * np.sin(2 * np.pi * 90 * samples) + 0.25 * np.sin(2 * np.pi * 180 * samples)
    )
    decisions = [False] * 25 + [True] * (frame_count - 25)

    capture = detect_phrase(split_frames(signal), vad=SequenceVad(decisions))

    assert not capture.accepted


def test_voice_band_command_is_accepted_over_road_noise():
    frame_count = 70
    samples = np.arange(320 * frame_count) / 16_000
    signal = 2500 * (
        0.75 * np.sin(2 * np.pi * 90 * samples) + 0.25 * np.sin(2 * np.pi * 180 * samples)
    )
    start, end = 320 * 30, 320 * 43
    signal[start:end] += 10_000 * (
        0.65 * np.sin(2 * np.pi * 600 * samples[start:end])
        + 0.35 * np.sin(2 * np.pi * 1200 * samples[start:end])
    )

    capture = detect_phrase(split_frames(signal), vad=SequenceVad([True] * frame_count))

    assert capture.accepted
    assert capture.voiced_ms == 260


def test_suspended_stream_drops_callback_audio_and_clears_stale_frames():
    stream = MicStream.__new__(MicStream)
    stream._queue = queue.Queue(maxsize=2)
    stream._frame_carry = b"stale"
    stream._capture_lock = threading.Lock()
    stream._suspended = threading.Event()

    stream._queue.put_nowait(b"queued")
    stream.suspend()
    stream._callback(b"tts", 0, None, None)

    assert stream._frame_carry == b""
    assert stream._queue.empty()

    stream.resume()
    stream._callback(b"voice", 0, None, None)

    assert stream._queue.get_nowait() == b"voice"
