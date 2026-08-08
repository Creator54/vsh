import os
import shlex
import signal
import subprocess
import threading


class CliThinker:
    """Run a command-line tool with the prompt as input."""

    def __init__(self, command: str, timeout: float = 15, **kwargs):
        self.command = command
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._process = None

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _close_pipes(process: subprocess.Popen) -> None:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    def cancel(self) -> None:
        """Cancel the active command and prevent new commands during shutdown."""
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._kill(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            self._close_pipes(process)

    def ask(self, prompt: str) -> str:
        if not prompt.strip():
            return "echo 'I did not catch that.'\n"
        if self._cancelled.is_set():
            return ""
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
            with self._lock:
                self._process = process
                cancelled = self._cancelled.is_set()
            if cancelled:
                self._kill(process)
            try:
                stdout, stderr = process.communicate(input=stdin_input, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._kill(process)
                try:
                    process.communicate(timeout=1)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    self._close_pipes(process)
                return "echo 'Command timed out'\n"
            except (OSError, ValueError):
                if self._cancelled.is_set():
                    return ""
                raise
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None
            if self._cancelled.is_set():
                return ""
            return (stdout or stderr or "").strip()
        except FileNotFoundError:
            return f"echo {shlex.quote('Command not found: ' + self.command)}\n"
        except subprocess.SubprocessError as e:
            return f"echo {shlex.quote('Subprocess error: ' + str(e))}\n"
