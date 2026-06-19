from abc import ABC, abstractmethod
from collections.abc import Iterator
from enum import Enum

import numpy as np
from loguru import logger


class VSHState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


def update_state(state: VSHState):
    """Log the current state transition."""
    logger.info(f"State changed: {state.value}")


class STTProvider(ABC):
    """Abstract Base Class for Speech-to-Text providers."""

    @abstractmethod
    def transcribe_stream(self, audio_stream: Iterator[bytes]) -> str:
        """Transcribe a stream of audio bytes into text."""
        pass

    @abstractmethod
    def transcribe_file(self, file_path: str) -> str:
        """Transcribe an audio file into text."""
        pass


class TTSProvider(ABC):
    """Abstract Base Class for Text-to-Speech providers."""

    @abstractmethod
    def synthesize(self, text: str) -> np.ndarray:
        """Synthesize text into an audio waveform (numpy array)."""
        pass


class Thinker(ABC):
    """Abstract Base Class for thinking/interpreting providers."""

    @abstractmethod
    def ask(self, prompt: str) -> str:
        """Ask a question and get a plain text response back."""
        pass
