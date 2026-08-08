import os
import shlex
import signal
import subprocess


class CliThinker:
    """Run a command-line tool with the prompt as input."""

    def __init__(self, command: str, timeout: float = 15, **kwargs):
        self.command = command
        self.timeout = timeout

    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "echo 'I did not catch that.'\n"
        try:
            cmd = self.command
            stdin_input: str | None = prompt

            if "{}" in cmd:
                cmd = cmd.replace("{}", shlex.quote(prompt))
                stdin_input = None

            process = subprocess.Popen(
                cmd,
                shell=True,
                stdin=subprocess.PIPE if stdin_input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(input=stdin_input, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                return "echo 'Command timed out'\n"
            return (stdout or stderr or "").strip()
        except FileNotFoundError:
            return f"echo {shlex.quote('Command not found: ' + self.command)}\n"
        except subprocess.SubprocessError as e:
            return f"echo {shlex.quote('Subprocess error: ' + str(e))}\n"
