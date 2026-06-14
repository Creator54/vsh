import threading
import queue
import time
from loguru import logger
from vsh.core.audio import MicStream
from vsh.providers import STT_PROVIDERS

class VoiceInputThread(threading.Thread):
    def __init__(self, stt_queue: queue.Queue, provider_name: str = "vosk"):
        super().__init__(name="VoiceInputThread")
        self.daemon = False  # Ensure cleanup on exit
        self.stt_queue = stt_queue
        self.provider_name = provider_name
        
        self.is_listening = False
        self.should_exit = False
        self.model_loaded = False
        self.stt_provider = None
        
        # Events to coordinate toggling without busy loops
        self._toggle_event = threading.Event()

    def load_model(self):
        """Lazy load the STT model on first use."""
        if not self.model_loaded:
            logger.info(f"Loading STT model ({self.provider_name})...")
            if self.provider_name in STT_PROVIDERS:
                self.stt_provider = STT_PROVIDERS[self.provider_name]()
            else:
                raise ValueError(f"Unknown STT provider: {self.provider_name}")
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
                with MicStream() as stream:
                    # Inner loop for the active microphone session
                    while self.is_listening and not self.should_exit:
                        # stream.live_gen blocks until VAD detects speech, 
                        # then yields chunks until silence
                        
                        audio_chunks = list(stream.live_gen(timeout=5))
                        
                        if audio_chunks and self.is_listening:
                            # Transcribe the accumulated speech
                            text = self.stt_provider.transcribe_stream(iter(audio_chunks))
                            text = text.strip()
                            if text:
                                self.stt_queue.put(text)
                                
            except Exception as e:
                logger.error(f"Voice thread error: {e}")
                time.sleep(1) # Prevent rapid crash loops
                
        logger.debug("Voice input thread exiting cleanly.")
