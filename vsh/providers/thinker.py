from vsh.core.provider import Thinker, ThinkerResponse

class EchoThinker(Thinker):
    """Simple thinker that echos back with a prefix."""
    def ask(self, prompt: str) -> ThinkerResponse:
        if not prompt.strip():
            return ThinkerResponse(command="", speech="I didn't catch that.")
        # Echo to terminal as text, and also speak it
        return ThinkerResponse(command=f"echo '{prompt}'", speech=f"You said: {prompt}")

