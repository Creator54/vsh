import warnings
# ponytail: silence deprecation noise at source
warnings.filterwarnings("ignore", category=DeprecationWarning)
import audioop
import wave
import os
import sys
import contextlib
import pyaudio
import numpy as np
from loguru import logger
import queue

@contextlib.contextmanager
def no_stderr():
    """Aggressively redirect stderr at the OS level to silence low-level library noise."""
    fd = sys.stderr.fileno()
    with os.fdopen(os.dup(fd), 'w') as saved:
        with open(os.devnull, 'w') as devnull:
            sys.stderr.flush()
            os.dup2(devnull.fileno(), fd)
            try: yield
            finally:
                sys.stderr.flush()
                os.dup2(saved.fileno(), fd)

class AudioSignal:
    def __init__(self, data: bytes, rate: int, width: int = 2):
        self.data, self.rate, self.width = data, rate, width
    def to_rate(self, target: int):
        if self.rate == target: return self
        d, _ = audioop.ratecv(self.data, self.width, 1, self.rate, target, None)
        return AudioSignal(d, target, self.width)
    def play(self, device_index=None):
        with no_stderr():
            pa = pyaudio.PyAudio()
            s = pa.open(format=pyaudio.paInt16, channels=1, rate=self.rate, output=True, output_device_index=device_index)
        s.write(self.data); s.stop_stream(); s.close(); pa.terminate()
    def save(self, p: str):
        with wave.open(p, 'wb') as f: f.setnchannels(1); f.setsampwidth(self.width); f.setframerate(self.rate); f.writeframes(self.data)

class MicStream:
    """Microphone audio stream using PyAudio."""
    def __init__(self, rate=16000, chunk=1024, device_index=None):
        self.rate, self.chunk, self.device_index = rate, chunk, device_index
        with no_stderr(): self._audio = pyaudio.PyAudio()
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

    def live_gen(self, silence_limit=15, timeout=50, threshold=400, verbose=False):
        """Generator that yields chunks until silence is detected."""
        silent_chunks = 0
        has_speech = False
        while True:
            chunk = self._queue.get()
            if chunk is None: break
            yield chunk
            
            energy = audioop.rms(chunk, 2)
            if verbose:
                # VU meter: 1 bar per 100 RMS, up to 3000
                bars = min(30, energy // 100)
                meter = "|" + "#" * bars + " " * (30 - bars) + "|"
                sys.stderr.write(f"\r{meter} {energy:5} (thr:{threshold})")
                sys.stderr.flush()
            
            if energy > threshold:
                if not has_speech and verbose: sys.stderr.write("\n[vsh] Speech detected...\n")
                has_speech = True
                silent_chunks = 0
            else:
                silent_chunks += 1
            
            if (has_speech and silent_chunks > silence_limit) or (not has_speech and silent_chunks > timeout):
                if verbose: sys.stderr.write("\n")
                break

if __name__ == "__main__":
    # ponytail: quick check if audio device is accessible
    try:
        with MicStream() as stream:
            logger.info("Recording for 1 second...")
            for i, chunk in enumerate(stream.live_gen(timeout=10)):
                if i > 10: break
            logger.success("Audio capture OK.")
    except Exception as e:
        logger.error(f"Audio error: {e}")
