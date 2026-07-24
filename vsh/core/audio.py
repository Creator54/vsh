import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
import contextlib  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import wave  # noqa: E402
from collections import deque  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import numpy as np  # noqa: E402
import pyaudio  # noqa: E402
from loguru import logger  # noqa: E402

_VOICE_BAND_MIN_RATIO = 0.20
_VOICE_BAND_SMOOTH_FRAMES = 5


@dataclass(frozen=True)
class PhraseCapture:
    chunks: tuple[bytes, ...] = ()
    voiced_ms: int = 0
    reason: str = "silence"
    accepted: bool = False


def detect_phrase(
    chunks,
    *,
    rate: int = 16000,
    threshold: int = 1000,
    silence_limit: int = 15,
    idle_timeout_ms: int = 3200,
    max_phrase_ms: int = 30000,
    frame_ms: int = 20,
    pre_roll_ms: int = 200,
    min_speech_ms: int = 260,
    calibration_ms: int = 500,
    noise_multiplier: float = 1.8,
    vad=None,
    stop_check=None,
    ignore_check=None,
    volume_callback=None,
    activity_callback=None,
    noise_energies=None,
) -> PhraseCapture:
    """Return one detected phrase from raw audio chunks."""
    if vad is None:
        import webrtcvad

        vad = webrtcvad.Vad(3)

    frame_bytes = rate * frame_ms // 1000 * 2
    frame_samples = frame_bytes // 2
    spectral_window = np.hanning(frame_samples)
    frequencies = np.fft.rfftfreq(frame_samples, 1 / rate)
    audible_band = (frequencies >= 80) & (frequencies <= 4000)
    voice_band = (frequencies >= 300) & (frequencies <= 3400)
    pre_roll = deque(maxlen=max(1, pre_roll_ms // frame_ms))
    trigger_window = deque(maxlen=8)
    voice_band_window = deque(maxlen=_VOICE_BAND_SMOOTH_FRAMES)
    if noise_energies is None:
        noise_energies = deque(maxlen=50)
    captured = []
    carry = b""
    triggered = False
    activity_announced = False
    voiced_frames = 0
    idle_frames = 0
    trailing_unvoiced = 0
    previous_sample = 0.0
    max_frames = max_phrase_ms // frame_ms
    idle_limit = idle_timeout_ms // frame_ms
    calibration_frames = calibration_ms // frame_ms
    minimum_threshold = max(100, threshold // 5)

    def finish(reason: str) -> PhraseCapture:
        accepted = triggered and voiced_frames * frame_ms >= min_speech_ms and reason != "cancelled"
        if activity_announced and activity_callback:
            activity_callback(False)
        return PhraseCapture(
            chunks=tuple(captured) if accepted else (),
            voiced_ms=voiced_frames * frame_ms if accepted else 0,
            reason=reason,
            accepted=accepted,
        )

    for chunk in chunks:
        if stop_check and stop_check():
            return finish("cancelled")
        carry += chunk
        while len(carry) >= frame_bytes:
            if stop_check and stop_check():
                return finish("cancelled")
            frame, carry = carry[:frame_bytes], carry[frame_bytes:]
            if ignore_check and ignore_check():
                if activity_announced and activity_callback:
                    activity_callback(False)
                pre_roll.clear()
                trigger_window.clear()
                voice_band_window.clear()
                captured.clear()
                triggered = False
                activity_announced = False
                voiced_frames = 0
                idle_frames = 0
                trailing_unvoiced = 0
                previous_sample = 0.0
                continue
            samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            raw_energy = int(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0
            # Pre-emphasis keeps low-frequency fan noise from masking speech onsets.
            filtered = np.empty_like(samples)
            filtered[0] = samples[0] - 0.97 * previous_sample
            filtered[1:] = samples[1:] - 0.97 * samples[:-1]
            previous_sample = samples[-1]
            filtered_pcm = np.clip(filtered, -32768, 32767).astype(np.int16)
            energy = int(np.sqrt(np.mean(np.square(filtered_pcm.astype(np.float32)))))
            power = np.square(np.abs(np.fft.rfft(samples * spectral_window)))
            audible_energy = float(power[audible_band].sum())
            voice_ratio = float(power[voice_band].sum() / audible_energy) if audible_energy > 0 else 0.0
            voice_band_window.append(voice_ratio)
            voice_like = voice_ratio >= _VOICE_BAND_MIN_RATIO
            sustained_voice = (
                len(voice_band_window) == voice_band_window.maxlen
                and statistics.median(voice_band_window) >= _VOICE_BAND_MIN_RATIO
            )
            vad_speech = vad.is_speech(frame, rate)
            calibrating = len(noise_energies) < calibration_frames
            if calibrating:
                noise_energies.append(energy)
            noise_threshold = int(float(np.percentile(noise_energies, 95)) * noise_multiplier)
            adaptive_threshold = max(minimum_threshold, noise_threshold)
            speech_frame = not calibrating and vad_speech and voice_like and energy >= adaptive_threshold
            confirmed_onset = speech_frame and sustained_voice
            if not calibrating and not triggered and not voice_like and energy > 0:
                noise_energies.append(energy)
            if volume_callback:
                volume_callback(raw_energy, adaptive_threshold)

            if not triggered:
                idle_frames += 1
                pre_roll.append((frame, speech_frame))
                trigger_window.append(confirmed_onset)
                if sum(trigger_window) >= 5:
                    triggered = True
                    captured.extend(item for item, _ in pre_roll)
                    voiced_frames = sum(1 for _, voiced in pre_roll if voiced)
                    if voiced_frames * frame_ms >= min_speech_ms and activity_callback:
                        activity_callback(True)
                        activity_announced = True
                    if len(captured) >= max_frames:
                        return finish("max_phrase")
                    continue
                if idle_frames >= idle_limit:
                    return finish("timeout")
                continue

            captured.append(frame)
            if speech_frame:
                voiced_frames += 1
                if not activity_announced and voiced_frames * frame_ms >= min_speech_ms:
                    if activity_callback:
                        activity_callback(True)
                    activity_announced = True
                trailing_unvoiced = 0
            else:
                trailing_unvoiced += 1
                if trailing_unvoiced >= silence_limit:
                    return finish("silence")
            if len(captured) >= max_frames:
                return finish("max_phrase")

    return finish("eof")


@contextlib.contextmanager
def no_stderr():
    """Silence low-level audio library warnings."""
    fd = sys.stderr.fileno()
    with os.fdopen(os.dup(fd), "w") as saved:
        with open(os.devnull, "w") as devnull:
            sys.stderr.flush()
            os.dup2(devnull.fileno(), fd)
            try:
                yield
            finally:
                sys.stderr.flush()
                os.dup2(saved.fileno(), fd)


def play_audio(data: bytes, rate: int, width: int = 2, device_index=None):
    with no_stderr():
        pa = pyaudio.PyAudio()
        try:
            s = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, output=True, output_device_index=device_index)
            try:
                s.write(data)
            finally:
                s.stop_stream()
                s.close()
        finally:
            pa.terminate()


def save_audio(p: str, data: bytes, rate: int, width: int = 2):
    with wave.open(p, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(width)
        f.setframerate(rate)
        f.writeframes(data)


class MicStream:
    """Microphone audio stream using PyAudio."""

    def __init__(self, rate=16000, chunk=1024, device_index=None):
        self.rate, self.chunk, self.device_index = rate, chunk, device_index
        with no_stderr():
            self._audio = pyaudio.PyAudio()
        self._queue = queue.Queue(maxsize=max(8, rate * 2 // chunk))
        self._stream = None
        self._frame_carry = b""
        self._noise_energies = deque(maxlen=50)
        self._capture_lock = threading.Lock()
        self._suspended = threading.Event()

    def __enter__(self):
        with no_stderr():
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk,
                stream_callback=self._callback,
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        self._audio.terminate()
        with self._capture_lock:
            self._clear_pending_locked()
            self._queue.put_nowait(None)

    def _callback(self, in_data, frame_count, time_info, status):
        with self._capture_lock:
            if self._suspended.is_set():
                return None, pyaudio.paContinue
            if self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(in_data)
        return None, pyaudio.paContinue

    def _clear_pending_locked(self):
        self._frame_carry = b""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def suspend(self):
        """Drop microphone input until capture is resumed."""
        with self._capture_lock:
            self._suspended.set()
            self._clear_pending_locked()

    def resume(self):
        """Resume with only audio recorded after this call."""
        with self._capture_lock:
            self._clear_pending_locked()
            self._suspended.clear()

    def _frames(self, stop_check=None):
        frame_bytes = self.rate * 20 // 1000 * 2
        while True:
            if stop_check and stop_check():
                return
            with self._capture_lock:
                if len(self._frame_carry) >= frame_bytes:
                    frame = self._frame_carry[:frame_bytes]
                    self._frame_carry = self._frame_carry[frame_bytes:]
                else:
                    frame = None
            if frame is not None:
                yield frame
                if stop_check and stop_check():
                    return
                continue
            try:
                chunk = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                return
            if stop_check and stop_check():
                return
            with self._capture_lock:
                if not self._suspended.is_set():
                    self._frame_carry += chunk

    def capture_phrase(
        self,
        silence_limit=15,
        threshold=1000,
        verbose=False,
        stop_check=None,
        ignore_check=None,
        volume_callback=None,
        activity_callback=None,
    ) -> PhraseCapture:
        """Capture one confirmed phrase without forwarding the idle wait."""

        def report_volume(energy: int, current_threshold: int):
            if volume_callback:
                volume_callback(energy, current_threshold)
            if verbose:
                bars = min(30, energy // 200)
                meter = "|" + "#" * bars + " " * (30 - bars) + "|"
                sys.stderr.write(f"\r{meter} {energy:5} (thr:{current_threshold})")
                sys.stderr.flush()

        capture = detect_phrase(
            self._frames(stop_check),
            rate=self.rate,
            threshold=threshold,
            silence_limit=silence_limit,
            stop_check=stop_check,
            ignore_check=ignore_check,
            volume_callback=report_volume,
            activity_callback=activity_callback,
            noise_energies=self._noise_energies,
        )
        if verbose:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
        return capture


if __name__ == "__main__":
    # quick check if audio device is accessible
    try:
        with MicStream() as stream:
            logger.info("Recording for 1 second...")
            stream.capture_phrase()
            logger.success("Audio capture OK.")
    except Exception as e:
        logger.error(f"Audio error: {e}")
