from abc import ABC, abstractmethod
from typing import Iterator
from enum import Enum
from loguru import logger
import numpy as np

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

from dataclasses import dataclass

@dataclass
class ThinkerResponse:
    command: str = ""
    speech: str = ""

class Thinker(ABC):
    """Abstract Base Class for LLM/Thinking providers."""

    # This prompt tells the LLM how to use its dual-channel output.
    # Note: Users can override this to request JSON, TOON, or any other format
    # as long as their Thinker implementation parses it into a ThinkerResponse.
    SYSTEM_PROMPT = """You are a voice assistant connected to a user's terminal. 
The user will speak to you. You can respond by typing a command into their terminal, 
speaking to them, or both.
- To execute a command, put it in the "command" field.
- To talk to the user, put it in the "speech" field.
- If you only want to speak (e.g., explaining something), leave "command" empty.
- If you only want to act without chattering, leave "speech" empty.
Return ONLY valid structured data."""

    @abstractmethod
    def ask(self, prompt: str) -> ThinkerResponse | str:
        """Ask a question and get a response."""
        pass

