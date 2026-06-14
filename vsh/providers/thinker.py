from vsh.core.provider import Thinker

class EchoThinker(Thinker):
    """Simple thinker that echos back with a prefix."""
    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "I didn't catch that."
        return f"VSH: {prompt}"
