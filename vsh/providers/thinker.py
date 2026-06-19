import shlex

from vsh.core.provider import Thinker


class EchoThinker(Thinker):
    """Simple thinker that echoes back with a prefix."""

    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "echo 'I didn\\'t catch that.'"
        return f"echo {shlex.quote('You said: ' + prompt)}"
