import shlex
import subprocess


class CliThinker:
    """Subprocess-based thinker that runs any CLI command with the prompt as stdin."""

    def __init__(self, command: str, timeout: float = 15, **kwargs):
        self.command = command
        self.timeout = timeout

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

            options = {
                "capture_output": True,
                "text": True,
                "timeout": self.timeout,
            }
            if stdin_input is None:
                options["stdin"] = subprocess.DEVNULL
            else:
                options["input"] = stdin_input

            result = subprocess.run(cmd, shell=True, **options)
            return (result.stdout or result.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return "echo 'Command timed out'\n"
        except FileNotFoundError:
            return f"echo {shlex.quote('Command not found: ' + self.command)}\n"
        except subprocess.SubprocessError as e:
            return f"echo {shlex.quote('Subprocess error: ' + str(e))}\n"
