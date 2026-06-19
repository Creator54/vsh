import shlex
import subprocess

from vsh.core.provider import Thinker


class CliThinker(Thinker):
    """Subprocess-based thinker that runs any CLI command with the prompt as stdin."""

    def __init__(self, command: str, **kwargs):
        self.command = command

    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "echo 'I did not catch that.'\n"
        try:
            cmd = self.command
            stdin_input = prompt
            
            # Support {} templating for tools that take prompt as argument
            if "{}" in cmd:
                cmd = cmd.replace("{}", shlex.quote(prompt))
                stdin_input = None

            result = subprocess.run(
                cmd,
                shell=True,
                input=stdin_input,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return (result.stdout or result.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return "echo 'Command timed out'\n"
        except FileNotFoundError:
            return f"echo {shlex.quote('Command not found: ' + self.command)}\n"
        except subprocess.SubprocessError as e:
            return f"echo {shlex.quote('Subprocess error: ' + str(e))}\n"
