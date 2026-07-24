import queue
import re
import threading
import time
from enum import StrEnum

from loguru import logger

_SILENCE_HALLUCINATIONS = {
    "subtitles by the amara org community",
    "thank you for watching",
    "thanks for watching",
}


class VoiceState(StrEnum):
    MUTED = "muted"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    TYPING = "typing"
    SPEAKING = "speaking"


def _is_silence_hallucination(text: str) -> bool:
    """Recognize filler captions commonly produced from non-speech."""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return normalized in _SILENCE_HALLUCINATIONS


def _is_valid_transcript(text: str) -> bool:
    return any(character.isalnum() for character in text) and not _is_silence_hallucination(text)


class VoiceInputThread(threading.Thread):
    def __init__(
        self,
        stt_queue: queue.Queue,
        config=None,
        device_index=None,
        verbose=False,
        vad_threshold=1000,
        vad_silence_limit=15,
        volume_callback=None,
        state_callback=None,
    ):
        super().__init__(name="VoiceInputThread")
        self.daemon = False
        self.stt_queue = stt_queue
        self.config = config
        self.device_index = device_index
        self.verbose = verbose
        self.vad_threshold = vad_threshold
        self.vad_silence_limit = vad_silence_limit
        self.volume_callback = volume_callback
        self.state_callback = state_callback

        self.is_listening = False
        self.should_exit = False
        self.model_loaded = False
        self.stt_provider = None
        self.is_processing = False
        self.system_mic_muted = None

        self._toggle_event = threading.Event()
        self._suppress_until = 0.0
        self._processing_lock = threading.Lock()
        self._active_stream = None

    def suppress_input(self, duration: float = 0.6):
        """Ignore brief audio caused by a known keypress."""
        self._suppress_until = max(self._suppress_until, time.monotonic() + duration)

    def _input_suppressed(self) -> bool:
        return time.monotonic() < self._suppress_until

    def load_model(self):
        """Load the speech model on first use."""
        if not self.model_loaded:
            provider_name = self.config.stt.provider if self.config else "vosk"
            logger.info(f"Loading STT model ({provider_name})...")
            from vsh.core.config import VshConfig
            from vsh.providers import resolve_stt

            config_to_use = self.config if self.config else VshConfig()
            self.stt_provider = resolve_stt(config_to_use)
            if not self.stt_provider:
                raise ValueError(f"Unknown STT provider: {provider_name}")
            self.model_loaded = True
            logger.info("STT model loaded.")

    def toggle_listening(self) -> bool:
        """Toggle listening state and return the new state."""
        self.is_listening = not self.is_listening
        self._toggle_event.set()
        if self.state_callback:
            self.state_callback(VoiceState.IDLE if self.is_listening else None)
        return self.is_listening

    def set_system_mic_muted(self, muted: bool | None):
        self.system_mic_muted = muted
        self._toggle_event.set()

    def stop(self):
        """Signal the thread to shut down completely."""
        self.should_exit = True
        self.is_listening = False
        self._toggle_event.set()

    def _capture_enabled(self) -> bool:
        return self.is_listening and self.system_mic_muted is not True

    def _resting_state(self) -> VoiceState | None:
        return VoiceState.IDLE if self.is_listening else None

    def _set_active_stream(self, stream):
        with self._processing_lock:
            self._active_stream = stream
            if stream is not None and self.is_processing:
                stream.suspend()

    def set_processing(self, processing: bool):
        """Pause capture while a voice request is being handled."""
        with self._processing_lock:
            if self.is_processing == processing:
                return
            self.is_processing = processing
            if self._active_stream is not None:
                if processing:
                    self._active_stream.suspend()
                else:
                    self._active_stream.resume()

    def _finish_processing(self):
        self.set_processing(False)
        if self.state_callback:
            self.state_callback(self._resting_state())

    def process_once(self, stream) -> bool:
        """Capture and transcribe one phrase, returning whether it was queued."""
        if not self._capture_enabled():
            self._finish_processing()
            return False

        def activity_changed(active: bool):
            if active and self.state_callback:
                self.state_callback(VoiceState.LISTENING)

        capture = stream.capture_phrase(
            threshold=self.vad_threshold,
            silence_limit=self.vad_silence_limit,
            verbose=self.verbose,
            stop_check=lambda: not self._capture_enabled() or self.is_processing,
            ignore_check=self._input_suppressed,
            volume_callback=self.volume_callback,
            activity_callback=activity_changed,
        )
        if not capture.accepted or not self._capture_enabled():
            self._finish_processing()
            return False

        self.set_processing(True)
        if self.state_callback:
            self.state_callback(VoiceState.TRANSCRIBING)
        if self.verbose:
            logger.info(f"Captured phrase: {len(capture.chunks)} frames")

        try:
            text = self.stt_provider.transcribe_stream(iter(capture.chunks)).strip()
        except Exception:
            self._finish_processing()
            raise

        if not self._capture_enabled():
            self._finish_processing()
            return False
        if not _is_valid_transcript(text):
            if text:
                logger.debug("Ignored probable non-speech transcript: {!r}", text)
            self._finish_processing()
            return False

        self.stt_queue.put(text)
        return True

    def run(self):
        while not self.should_exit:
            if not self._capture_enabled():
                self._toggle_event.wait()
                self._toggle_event.clear()
                continue

            if self.should_exit:
                break

            try:
                self.load_model()
                if not self._capture_enabled():
                    continue
                from vsh.core.audio import MicStream, no_stderr

                with no_stderr(), MicStream(device_index=self.device_index) as stream:
                    self._set_active_stream(stream)
                    try:
                        while self._capture_enabled() and not self.should_exit:
                            if self.is_processing:
                                time.sleep(0.1)
                                continue

                            self.process_once(stream)
                    finally:
                        self._set_active_stream(None)

            except Exception as e:
                self._finish_processing()
                logger.error(f"Voice thread error: {e}")
                time.sleep(1)  # Avoid a tight retry loop after device failures.

        logger.debug("Voice input thread exiting cleanly.")
