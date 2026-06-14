from loguru import logger
import pyaudio
import queue
import wave
import numpy as np
import audioop

class MicStream:
    """Microphone audio stream using PyAudio."""
    def __init__(self, rate=16000, chunk=1024):
        self.rate = rate
        self.chunk = chunk
        self._audio = pyaudio.PyAudio()
        self._queue = queue.Queue()
        self._stream = None

    def __enter__(self):
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.rate,
            input=True,
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

    def live_gen(self, silence_limit=20, timeout=50, threshold=800):
        """Generator that yields chunks until silence is detected."""
        silent_chunks = 0
        has_speech = False
        while True:
            chunk = self._queue.get()
            if chunk is None: break
            yield chunk
            
            # ponytail: stdlib audioop is faster than numpy for RMS
            energy = audioop.rms(chunk, 2) # 2 bytes for int16
            
            if energy > threshold:
                has_speech = True
                silent_chunks = 0
            else:
                silent_chunks += 1
            
            if (has_speech and silent_chunks > silence_limit) or (not has_speech and silent_chunks > timeout):
                break

def play_audio(data: np.ndarray, rate=44100):
    """Play a numpy array as audio."""
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paFloat32, channels=1, rate=rate, output=True)
    stream.write(data.astype(np.float32).tobytes())
    stream.stop_stream()
    stream.close()
    p.terminate()

def save_audio(data: np.ndarray, file_path: str, rate=44100):
    """Save a numpy array as a WAV file using stdlib wave."""
    if data.dtype in (np.float32, np.float64):
        data = (data * 32767).astype(np.int16)
    with wave.open(file_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2) # 16-bit
        wf.setframerate(rate)
        wf.writeframes(data.tobytes())

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
