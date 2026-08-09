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
        capture_started_callback=None,
        capture_stopped_callback=None,
        error_callback=None,
    ):
        super().__init__(name="VoiceInputThread", daemon=True)
        self.stt_queue = stt_queue
        self.config = config
        self.device_index = device_index
        self.verbose = verbose
        self.vad_threshold = vad_threshold
        self.vad_silence_limit = vad_silence_limit
        self.volume_callback = volume_callback
        self.state_callback = state_callback
        self.capture_started_callback = capture_started_callback
        self.capture_stopped_callback = capture_stopped_callback
        self.error_callback = error_callback

        self.is_listening = False
        self.should_exit = False
        self.model_loaded = False
        self.stt_provider = None
        self.is_processing = False
        self.system_mic_muted: bool | None = None

        self._toggle_event = threading.Event()
        self._toggle_lock = threading.Lock()
        self._suppress_until = 0.0
        self._processing_lock = threading.Lock()
        self._active_stream = None
        self._microphone_lease = None
        self._listen_generation = 0

    def suppress_input(self, duration: float = 0.6):
        """Ignore brief audio caused by a known keypress."""
        deadline = time.monotonic() + duration
        with self._processing_lock:
            self._suppress_until = max(self._suppress_until, deadline)

    def _input_suppressed(self) -> bool:
        now = time.monotonic()
        with self._processing_lock:
            return now < self._suppress_until

    def load_model(self):
        """Load the speech model on first use."""
        if not self.model_loaded:
            provider_name = self.config.stt.provider if self.config else "vosk"
            logger.info(f"Loading STT model ({provider_name})...")
            from vsh.core.config import VshConfig
            from vsh.providers import resolve_stt

            config_to_use = self.config if self.config else VshConfig()
            self.stt_provider = resolve_stt(config_to_use, raise_errors=True)
            if not self.stt_provider:
                raise ValueError(f"STT provider unavailable: {provider_name}")
            self.model_loaded = True
            logger.info("STT model loaded.")

    def toggle_listening(self) -> bool:
        """Toggle listening state and return the new state."""
        with self._toggle_lock:
            self.is_listening = not self.is_listening
            self._listen_generation += 1
            listening = self.is_listening
            if not listening:
                self._abort_active_stream()
            self._toggle_event.set()
            if self.state_callback:
                self.state_callback(VoiceState.IDLE if listening else None)
            return listening

    def set_system_mic_muted(self, muted: bool | None):
        with self._processing_lock:
            self.system_mic_muted = muted
            self._sync_stream_locked()
        self._toggle_event.set()

    def stop(self):
        """Signal the thread to shut down completely without blocking."""
        self.should_exit = True
        with self._toggle_lock:
            self.is_listening = False
            self._listen_generation += 1
        self._abort_active_stream()
        self._toggle_event.set()

    def _abort_active_stream(self):
        with self._processing_lock:
            stream = self._active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

    def _session_enabled(self, generation: int | None = None) -> bool:
        return self.is_listening and (generation is None or generation == self._listen_generation)

    def _disable_listening(self, generation: int) -> bool:
        with self._toggle_lock:
            if generation != self._listen_generation or not self.is_listening:
                return False
            self.is_listening = False
            self._listen_generation += 1
            if self.state_callback:
                self.state_callback(None)
            return True

    def _capture_enabled(self, generation: int | None = None) -> bool:
        return self._session_enabled(generation) and self.system_mic_muted is not True

    def _resting_state(self) -> VoiceState | None:
        return VoiceState.IDLE if self.is_listening else None

    def _set_active_stream(self, stream):
        with self._processing_lock:
            self._active_stream = stream
            if stream is not None and (self.is_processing or self.system_mic_muted is True):
                stream.suspend()

    def _sync_stream_locked(self):
        if self._active_stream is None:
            return
        if self.is_processing or self.system_mic_muted is True:
            self._active_stream.suspend()
        else:
            self._active_stream.resume()

    def set_processing(self, processing: bool):
        """Pause capture while a voice request is being handled."""
        with self._processing_lock:
            if self.is_processing == processing:
                return
            self.is_processing = processing
            self._sync_stream_locked()
        self._toggle_event.set()

    def _finish_processing(self):
        self.set_processing(False)
        if self.state_callback:
            self.state_callback(self._resting_state())

    def process_once(self, stream, generation: int | None = None) -> bool:
        """Capture and transcribe one phrase, returning whether it was queued."""
        if not self._capture_enabled(generation):
            self._finish_processing()
            return False

        def activity_changed(active: bool):
            if active and self.state_callback:
                self.state_callback(VoiceState.LISTENING)

        capture = stream.capture_phrase(
            threshold=self.vad_threshold,
            silence_limit=self.vad_silence_limit,
            verbose=self.verbose,
            stop_check=lambda: not self._capture_enabled(generation) or self.is_processing,
            ignore_check=self._input_suppressed,
            volume_callback=self.volume_callback,
            activity_callback=activity_changed,
        )
        if not capture.accepted or not self._capture_enabled(generation):
            self._finish_processing()
            return False

        self.set_processing(True)
        if self.state_callback:
            self.state_callback(VoiceState.TRANSCRIBING)
        if self.verbose:
            logger.info(f"Captured phrase: {len(capture.chunks)} frames")

        if self.stt_provider is None:
            logger.warning("No STT provider configured for voice input.")
            self._finish_processing()
            return False

        try:
            text = self.stt_provider.transcribe_stream(iter(capture.chunks)).strip()
        except Exception:
            self._finish_processing()
            raise

        if not self._capture_enabled(generation):
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
            if not self._session_enabled():
                self._toggle_event.wait()
                self._toggle_event.clear()
                continue

            if self.should_exit:
                break

            try:
                with self._toggle_lock:
                    generation = self._listen_generation
                try:
                    self.load_model()
                except Exception as e:
                    self._disable_listening(generation)
                    if self.error_callback:
                        self.error_callback(str(e))
                    else:
                        logger.error(f"Voice thread error: {e}")
                    continue
                if not self._session_enabled(generation):
                    continue

                from vsh.core.microphone import acquire_microphone, release_microphone

                lease = acquire_microphone()
                if lease is None:
                    disabled = self._disable_listening(generation)
                    if disabled:
                        logger.warning("Microphone is already in use by another VSH session")
                    continue

                with self._toggle_lock:
                    keep_lease = self._session_enabled(generation) and not self.should_exit
                    if keep_lease:
                        self._microphone_lease = lease
                if not keep_lease:
                    release_microphone(lease)
                    continue

                from vsh.core.audio import MicStream

                monitor_started = False
                try:
                    with MicStream(device_index=self.device_index) as stream:
                        self._set_active_stream(stream)
                        try:
                            if self.capture_started_callback:
                                self.capture_started_callback()
                            monitor_started = True
                            while self._session_enabled(generation) and not self.should_exit:
                                if not self._capture_enabled(generation) or self.is_processing:
                                    self._toggle_event.wait(timeout=0.1)
                                    self._toggle_event.clear()
                                    continue

                                self.process_once(stream, generation)
                        finally:
                            self._set_active_stream(None)
                finally:
                    try:
                        if monitor_started and self.capture_stopped_callback:
                            self.capture_stopped_callback()
                    finally:
                        with self._toggle_lock:
                            if self._microphone_lease is lease:
                                self._microphone_lease = None
                        release_microphone(lease)

            except Exception as e:
                self._finish_processing()
                logger.error(f"Voice thread error: {e}")
                time.sleep(1)  # Avoid a tight retry loop after device failures.

        logger.debug("Voice input thread exiting cleanly.")
