import queue
import threading
import time

from loguru import logger


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
        self.daemon = False  # Ensure cleanup on exit
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

        # Events to coordinate toggling without busy loops
        self._toggle_event = threading.Event()

    def load_model(self):
        """Lazy load the STT model on first use."""
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
        if self.is_listening:
            self.load_model()
            # Wake up the thread if it was waiting
            self._toggle_event.set()
        return self.is_listening

    def stop(self):
        """Signal the thread to shut down completely."""
        self.should_exit = True
        self.is_listening = False
        self._toggle_event.set()

    def run(self):
        while not self.should_exit:
            if not self.is_listening:
                # Wait until we are told to listen or exit
                self._toggle_event.wait()
                self._toggle_event.clear()
                continue

            if self.should_exit:
                break

            try:
                from vsh.core.audio import MicStream, no_stderr

                with no_stderr(), MicStream(device_index=self.device_index) as stream:
                    # Inner loop for the active microphone session
                    while self.is_listening and not self.should_exit:
                        if self.is_processing:
                            time.sleep(0.1)
                            continue

                        # Transition to actual phrase collection
                        audio_chunks = list(
                            stream.live_gen(
                                threshold=self.vad_threshold,
                                silence_limit=self.vad_silence_limit,
                                verbose=self.verbose,
                                stop_check=lambda: not self.is_listening or self.is_processing,
                                volume_callback=self.volume_callback,
                            )
                        )

                        if audio_chunks and self.is_listening and getattr(stream, "last_capture_had_speech", False):
                            # Log phrase capture
                            if self.verbose:
                                logger.info(f"Captured phrase: {len(audio_chunks)} chunks")

                            # Transcribe the accumulated speech
                            text = self.stt_provider.transcribe_stream(iter(audio_chunks))
                            text = text.strip()

                            if text:
                                # We have valid human speech! Lock the mic and show Processing.
                                if getattr(self, "state_callback", None):
                                    self.state_callback("transcribing", text=text)
                                self.stt_queue.put(text)
                            else:
                                # False alarm (e.g. table bump, cough, fan noise).
                                # Do nothing so the UI doesn't flicker, and let the mic naturally restart.
                                pass

            except Exception as e:
                logger.error(f"Voice thread error: {e}")
                time.sleep(1)  # Prevent rapid crash loops

        logger.debug("Voice input thread exiting cleanly.")
