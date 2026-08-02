import io
import wave

import numpy as np


def encode_pcm_wav(pcm: bytes, rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm)
    return output.getvalue()


def decode_pcm16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def decode_pcm16_wav(data: bytes) -> np.ndarray:
    return decode_pcm16_wav_with_rate(data)[0]


def decode_pcm16_wav_with_rate(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as stream:
        samples = decode_pcm16(stream.readframes(stream.getnframes()))
        return samples, stream.getframerate()


def decode_audio_to_pcm16(data: bytes) -> np.ndarray:
    """Decode audio bytes to float32 normalized PCM, parsing WAV headers or falling back to raw PCM16."""
    return decode_audio_to_pcm16_with_rate(data)[0]


def decode_audio_to_pcm16_with_rate(data: bytes, raw_rate: int = 44100) -> tuple[np.ndarray, int]:
    """Decode audio and retain a WAV sample rate, or use raw_rate for headerless PCM."""
    try:
        return decode_pcm16_wav_with_rate(data)
    except (wave.Error, EOFError, ValueError):
        return decode_pcm16(data), raw_rate


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    duration = len(audio) / source_rate
    target_length = int(duration * target_rate)
    source = np.linspace(0, duration, len(audio))
    target = np.linspace(0, duration, target_length)
    return np.interp(target, source, audio).astype(np.float32)
