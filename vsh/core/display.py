from enum import Enum
from loguru import logger

class VSHState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"

class DisplayManager:
    """Manages visual feedback for the voice shell."""
    
    def __init__(self):
        self.current_state = VSHState.IDLE

    def update_state(self, state: VSHState):
        """Update the current state and log it."""
        self.current_state = state
        logger.info(f"State changed: {state.value}")
