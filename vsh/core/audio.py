import warnings

# ponytail: silence deprecation noise at source
warnings.filterwarnings("ignore", category=DeprecationWarning)
import contextlib  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import sys  # noqa: E402
import wave  # noqa: E402

import numpy as np  # noqa: E402
import pyaudio  # noqa: E402
from loguru import logger  # noqa: E402


@contextlib.contextmanager
def no_stderr():
    """Aggressively redirect stderr at the OS level to silence low-level library noise."""
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
        s = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, output=True, output_device_index=device_index)
    s.write(data)
    s.stop_stream()
    s.close()
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
        self._queue = queue.Queue()
        self._stream = None

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
        self._queue.put(None)

    def _callback(self, in_data, frame_count, time_info, status):
        self._queue.put(in_data)
        return None, pyaudio.paContinue

    def live_gen(
        self, silence_limit=15, timeout=50, threshold=1000, verbose=False, stop_check=None, volume_callback=None
    ):
        """Generator that yields chunks until silence is detected."""
        silent_chunks = 0
        has_speech = False

        # Dynamic VAD state: Persist across generator restarts!
        if not hasattr(self, "_dynamic_noise_floor"):
            self._dynamic_noise_floor = threshold
            self._current_threshold = threshold

        consecutive_speech = 0
        consecutive_silence = 0
        ui_is_listening = False
        history = [False] * 5

        while True:
            if stop_check and stop_check():
                self.last_capture_had_speech = has_speech
                break
            try:
                chunk = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                self.last_capture_had_speech = has_speech
                break
            yield chunk

            data_np = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            energy = int(np.sqrt(np.mean(np.square(data_np)))) if len(data_np) > 0 else 0

            # Auto-calibrate threshold based on ambient noise
            if energy > self._current_threshold:
                consecutive_speech += 1
                consecutive_silence = 0
                if consecutive_speech > 30:
                    # 3 seconds of completely unbroken mechanical noise (e.g. a passing car or fan spike).
                    # Force adapt the noise floor.
                    self._dynamic_noise_floor = energy
                    self._current_threshold = max(threshold, int(self._dynamic_noise_floor * 2.0))
                    consecutive_speech = 0
            else:
                consecutive_speech = 0
                consecutive_silence += 1
                if energy > 0:  # Ignore pure 0s from OS-level mute so we don't ruin the calibration
                    # Use a slow Exponential Moving Average (EMA) to prevent single downward spikes
                    # (e.g. USB audio dropouts) from dragging the threshold down and causing false positives.
                    self._dynamic_noise_floor = (0.9 * self._dynamic_noise_floor) + (0.1 * energy)
                    self._current_threshold = max(threshold, int(self._dynamic_noise_floor * 2.0))

            # UI Debounce & Hangover logic
            history.append(energy > self._current_threshold)
            history.pop(0)

            if energy == 0:
                ui_is_listening = False
                display_energy = 0
            else:
                if sum(history) >= 3:
                    ui_is_listening = True
                elif consecutive_silence >= silence_limit:
                    ui_is_listening = False

                if ui_is_listening:
                    display_energy = max(energy, self._current_threshold + 1)
                else:
                    display_energy = min(energy, self._current_threshold)

            if volume_callback:
                volume_callback(display_energy, self._current_threshold)

            if verbose:
                # VU meter: 1 bar per 200 RMS, up to 6000
                bars = min(30, energy // 200)
                meter = "|" + "#" * bars + " " * (30 - bars) + "|"
                sys.stderr.write(f"\r{meter} {energy:5} (thr:{self._current_threshold})")
                sys.stderr.flush()

            if ui_is_listening:
                if not has_speech and verbose:
                    sys.stderr.write("\n[vsh] Speech detected...\n")
                has_speech = True
                silent_chunks = 0
            else:
                silent_chunks += 1
                if has_speech and consecutive_silence >= silence_limit:
                    # Break the moment the UI hangover drops, preventing 1.5s lag
                    if verbose:
                        sys.stderr.write("\r\033[K")  # Clear the diagnostic line
                    self.last_capture_had_speech = has_speech
                    break
                elif not has_speech and silent_chunks > timeout:
                    if verbose:
                        sys.stderr.write("\r\033[K")  # Clear the diagnostic line
                    self.last_capture_had_speech = has_speech
                    break


if __name__ == "__main__":
    # ponytail: quick check if audio device is accessible
    try:
        with MicStream() as stream:
            logger.info("Recording for 1 second...")
            for i, _chunk in enumerate(stream.live_gen(timeout=10)):
                if i > 10:
                    break
            logger.success("Audio capture OK.")
    except Exception as e:
        logger.error(f"Audio error: {e}")
