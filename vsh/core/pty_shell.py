import collections
import fcntl
import json
import os
import pty
import queue
import re
import secrets
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass

from loguru import logger

from vsh.core.config import VshConfig
from vsh.core.mic_state import PipeWireMicMonitor
from vsh.core.voice_indicator import CURSOR_RESET as CURSOR_RESET
from vsh.core.voice_indicator import VoiceIndicator
from vsh.core.voice_input import VoiceInputThread, VoiceState

_ANSI = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_][0-?]*[ -/]*[@-~]")


def _strip_ansi(b: bytes) -> bytes:
    return _ANSI.sub(b"", b)


def _strip_unicode(text: str) -> str:
    """Remove terminal symbols from captured command output."""
    return re.sub(r"[\u2300-\u25ff\u2700-\u27bf\ue000-\uf8ff]|[\ud800-\udbff][\udc00-\udfff]", "", text)


def _clean_output(raw: bytes) -> str:
    return _strip_unicode(_strip_ansi(raw).decode("utf-8", "replace"))


@dataclass(frozen=True)
class VoiceReply:
    speech: str = ""
    command: str = ""


def parse_voice_reply(raw: str) -> VoiceReply:
    """Read a structured voice reply, falling back to plain speech."""
    text = str(raw or "").strip()
    if not text:
        return VoiceReply()
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("voice reply must be an object")
        speech = payload.get("speech")
        command = payload.get("command")
        if speech is not None and not isinstance(speech, str):
            raise ValueError("speech must be a string or null")
        if command is not None and not isinstance(command, str):
            raise ValueError("command must be a string or null")
        return VoiceReply((speech or "").strip(), (command or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return VoiceReply(speech=text)


def _voice_prompt(transcript: str, mode: str) -> str:
    rules = {
        "command_only": "Set speech to an empty string and command to the requested shell command.",
        "speak_only": "Set speech to a concise response and command to null.",
        "speak_and_command": (
            "Set speech to a concise response. Set command to the shell command when one should run, otherwise null."
        ),
    }
    rule = rules.get(mode, rules["speak_and_command"])
    return (
        "Return exactly one JSON object and no markdown: "
        '{"speech":"brief response","command":null}. '
        "The command value must be either a shell command string or null. "
        f"{rule} Never put prose in command.\n\nUser request: {transcript}"
    )


class PtyShell:
    def __init__(
        self,
        config: VshConfig,
        thinker=None,
        verbose: bool = False,
        tts_provider=None,
        voice_handler=None,
    ):
        self.config = config
        self.thinker = thinker
        self.verbose = verbose
        self.tts_provider = tts_provider
        self.voice_handler = voice_handler

        k = self.config.keybinds.toggle_listen.lower()
        self.triggers = []

        if hasattr(self.config.keybinds, "toggle_listen_triggers") and self.config.keybinds.toggle_listen_triggers:
            for trigger_hex in self.config.keybinds.toggle_listen_triggers:
                try:
                    self.triggers.append(bytes.fromhex(trigger_hex))
                except Exception:
                    pass

        if not self.triggers:
            if k in ("ctrl+\\", "ctrl+\\\\"):
                self.triggers = [b"\x1c", b"\x1b[92;5u", b"\x1b[92;133u", b"\x1b[28;5u", b"\x1b[28;133u"]
            elif k == "ctrl+g":
                self.triggers = [b"\x07", b"\x1b[103;5u", b"\x1b[103;133u"]
            else:
                self.triggers = [b"\x1c", b"\x1b[92;5u", b"\x1b[92;133u", b"\x1b[28;5u", b"\x1b[28;133u"]

        # Keep Ctrl+G and Ctrl+] available alongside configured triggers.
        fallback_triggers = [b"\x07", b"\x1d", b"\x1b[103;5u", b"\x1b[103;133u"]
        for trigger in fallback_triggers:
            if trigger not in self.triggers:
                self.triggers.append(trigger)

        import shutil

        self.inner_shell = (
            config.shell.inner_shell
            or os.environ.get("SHELL")
            or shutil.which("bash")
            or shutil.which("sh")
            or "/bin/sh"
        )
        self.inner_shell_args = getattr(config.shell, "inner_shell_args", [self.inner_shell])

        self.master_fd = None
        self.stt_queue = queue.Queue()
        self.voice_thread = VoiceInputThread(
            self.stt_queue,
            config=self.config,
            device_index=self.config.stt.device_index,
            verbose=self.verbose,
            vad_threshold=self.config.stt.vad_threshold,
            vad_silence_limit=self.config.stt.vad_silence_limit,
            volume_callback=self._volume_callback,
            state_callback=self._set_cursor_state,
        )
        self.mic_monitor = PipeWireMicMonitor(self._set_system_mic_muted)
        self.pipeline_thread = None
        self.old_tty_attrs = None
        self._interrupted = False
        self.indicator = VoiceIndicator(config.shell.overlay_mode, config.stt.vad_threshold)
        self._current_cursor_state = None
        self._system_mic_muted = None

        self.shell_name = os.path.basename(self.inner_shell)
        self.shell_pid = None
        self.shell_state = "starting"
        self._exec_lock = threading.Lock()
        self._cap_buf = None

        self.output_history = collections.deque(maxlen=2000)

    def _pipeline_worker(self):
        """Process voice input without blocking the shell."""
        while True:
            transcript = self.stt_queue.get()
            if transcript is None:
                break

            if self.verbose:
                logger.info(f"Processing: {transcript}")

            processor = self.voice_handler or self.thinker
            if processor:
                self._set_cursor_state(VoiceState.THINKING)
                try:
                    prompt = transcript
                    if not self.voice_handler:
                        mode = getattr(self.config.llm, "output_mode", "speak_and_command")
                        prompt = _voice_prompt(transcript, mode)
                    self._dispatch_response(processor.ask(prompt))
                except Exception as e:
                    self._publish_reply(f"vsh: {e}", "")
                    self._set_cursor_state(self._resting_voice_state())
            else:
                self._inject_command(transcript)
                self._set_cursor_state(self._resting_voice_state())

            self.stt_queue.task_done()

    def _speak(self, text: str) -> bool:
        """Play the reply before running its command."""
        try:
            wav = self.tts_provider.synthesize(text)
            data = (wav * 32767 * 0.9).astype("int16").tobytes()
            from vsh.core.audio import play_audio

            play_audio(data, 44100, device_index=self.config.tts.device_index)
            return True
        except Exception as e:
            logger.error(f"TTS Error: {e}")
            return False

    def _dispatch_response(self, raw: str):
        """Speak or print the reply, then run its command."""
        reply = parse_voice_reply(raw)
        visible_speech = reply.speech
        if reply.speech and self.tts_provider:
            self._set_cursor_state(VoiceState.SPEAKING)
            spoke = self._speak(reply.speech)
            self.voice_thread.suppress_input(0.3)
            if spoke:
                visible_speech = ""
        if reply.command:
            self._set_cursor_state(VoiceState.TYPING)
        if visible_speech or reply.command:
            self._publish_reply(visible_speech, reply.command)
        self._set_cursor_state(self._resting_voice_state())

    def _setup_terminal(self):
        """Save terminal state and switch to direct input when a terminal exists."""
        if not os.isatty(sys.stdin.fileno()):
            self.old_tty_attrs = None
            return
        self.old_tty_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def _restore_terminal(self):
        """Remove the voice indicator and restore the original terminal state."""
        if not self.old_tty_attrs:
            return
        try:
            self.indicator.restore()
        except (OSError, ValueError, termios.error):
            pass
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, self.old_tty_attrs)
        except (OSError, ValueError, termios.error):
            pass

    def _handle_sigint(self, signum, frame):
        self._interrupted = True

    def _handle_sigwinch(self, signum, frame):
        """Pass terminal size changes to the shell."""
        if self.master_fd is None:
            return

        try:
            buf = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"0000")
            rows, cols = struct.unpack("hh", buf)
            self.indicator.resize(cols)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), cols, 0, 0))
        except Exception:
            pass

    def _volume_callback(self, energy: int, threshold: int):
        self.indicator.update_volume(energy, threshold)

    def _resting_voice_state(self) -> VoiceState | None:
        if self.voice_thread.is_listening:
            return VoiceState.IDLE
        return None

    def _effective_cursor_state(self) -> VoiceState | None:
        if not self.voice_thread.is_listening:
            return None
        if self._system_mic_muted is True:
            return VoiceState.MUTED
        state = self._current_cursor_state
        if state is not None:
            state = VoiceState(state)
        if state in (None, VoiceState.MUTED):
            return VoiceState.IDLE
        return state

    def _set_cursor_state(self, state: VoiceState | str | None):
        """Update pipeline state and its visual indicator."""
        state = VoiceState(state) if state is not None else None
        if state in (VoiceState.TRANSCRIBING, VoiceState.THINKING, VoiceState.TYPING, VoiceState.SPEAKING):
            if hasattr(self, "voice_thread"):
                self.voice_thread.set_processing(True)
        elif state in (None, VoiceState.MUTED, VoiceState.IDLE):
            if hasattr(self, "voice_thread"):
                self.voice_thread.set_processing(False)

        self._current_cursor_state = state
        self.indicator.render(self._effective_cursor_state())

    def _set_system_mic_muted(self, muted: bool | None):
        self._system_mic_muted = muted
        if hasattr(self, "voice_thread"):
            self.voice_thread.set_system_mic_muted(muted)
        self.indicator.render(self._effective_cursor_state())

    def voice_status(self) -> dict:
        state = self._effective_cursor_state()
        phase = self._current_cursor_state
        return {
            "enabled": self.voice_thread.is_listening,
            "mic_muted": self._system_mic_muted,
            "phase": str(phase) if phase is not None else None,
            "visual_state": str(state) if state is not None else None,
        }

    def _user_started_typing(self):
        self.voice_thread.suppress_input(0.6)
        self.indicator.user_started_typing()

    def _track_pty_controls(self, data: bytes):
        self.indicator.track_output(data)

    def _notify(self, msg: str, color="36"):
        """Verbose notification for startup and toggle diagnostics."""
        if self.verbose:
            sys.stdout.buffer.write(f"\r\n\033[{color}m[vsh]\033[0m {msg}\r\n".encode())
            sys.stdout.buffer.flush()

    def _toggle_listening(self):
        enabled = self.voice_thread.toggle_listening()
        if enabled:
            self.voice_thread.suppress_input(0.6)
        if self.verbose:
            m = f"LISTENING (Press {self.config.keybinds.toggle_listen} or Ctrl+G to pause)" if enabled else "STOPPED"
            c = "1;35" if enabled else "36"
            self._notify(m, color=c)
        if enabled:
            sys.stdout.buffer.write(b"\a")

    def _inject_command(self, cmd: str):
        """Send text directly to the shell."""
        if not cmd:
            return

        if getattr(self.config.shell, "auto_submit", False) and not cmd.endswith("\n"):
            cmd += "\n"

        try:
            os.write(self.master_fd, cmd.encode())
        except OSError as e:
            logger.error(f"Failed to write to PTY: {e}")

    @staticmethod
    def _write_bridge_file(path: str, content: str):
        pending = f"{path}.{secrets.token_hex(4)}.tmp"
        with open(pending, "x", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(pending, 0o600)
        os.replace(pending, path)

    def _publish_reply(self, speech: str, command: str):
        """Publish speech and command as one ordered terminal update."""
        is_fish = self.shell_name.lower().rsplit("-", 1)[-1] == "fish"
        if self.config.shell.response_bridge == "fish-signal" and self.shell_pid and is_fish:
            base = os.environ.get("XDG_RUNTIME_DIR")
            runtime = os.path.join(base, "vsh") if base else os.path.expanduser("~/.vsh/run")
            os.makedirs(runtime, mode=0o700, exist_ok=True)
            os.chmod(runtime, 0o700)
            paths = {
                "response": os.path.join(runtime, f"{self.shell_pid}.response"),
                "command": os.path.join(runtime, f"{self.shell_pid}.command"),
                "submit": os.path.join(runtime, f"{self.shell_pid}.submit"),
            }
            for path in paths.values():
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            if speech:
                self._write_bridge_file(paths["response"], speech.rstrip() + "\n")
            if command:
                self._write_bridge_file(paths["command"], command)
                if self.config.shell.auto_submit:
                    self._write_bridge_file(paths["submit"], "1\n")
            os.kill(self.shell_pid, signal.SIGUSR1)
            return

        if speech:
            sys.stdout.write(f"\r\n{speech.rstrip()}\r\n")
            sys.stdout.flush()
        if command:
            self._inject_command(command)

    def run(self):
        """Start the shell, background workers, and input/output loop."""
        os.environ["VSH_ACTIVE"] = "1"

        pid, self.master_fd = pty.fork()

        if pid == 0:
            try:
                os.execv(self.inner_shell, self.inner_shell_args)
            except Exception as e:
                sys.stderr.write(f"[vsh] Failed to start shell: {self.inner_shell}: {e}\n")
                os._exit(1)
        else:
            self.shell_pid = pid

            signal.signal(signal.SIGINT, self._handle_sigint)
            signal.signal(signal.SIGTERM, self._handle_sigint)
            signal.signal(signal.SIGHUP, self._handle_sigint)
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
            self._handle_sigwinch(None, None)

            self.mic_monitor.start()
            self.voice_thread.start()

            self.pipeline_thread = threading.Thread(target=self._pipeline_worker, daemon=True)
            self.pipeline_thread.start()

            self._setup_terminal()
            self.indicator.select_mode(self.old_tty_attrs is not None)

            def show_startup_ui():
                time.sleep(0.5)
                self.shell_state = "idle"
                if self.verbose:
                    self._notify(f"VSH active. Press {self.config.keybinds.toggle_listen} or Ctrl+G to toggle.")
                if self.config.shell.voice_on_start:
                    # The toggle renders immediately; injecting a wake-up byte would
                    # become unwanted input in the inner shell.
                    self._toggle_listening()

            threading.Thread(target=show_startup_ui, daemon=True).start()

            try:
                self._io_loop()
            finally:
                self.mic_monitor.stop()
                self.mic_monitor.join(timeout=2.0)
                self.voice_thread.stop()
                self.voice_thread.join(timeout=2.0)
                self._restore_terminal()
                self.stt_queue.put(None)
                for _ in range(50):
                    try:
                        wpid, _ = os.waitpid(pid, os.WNOHANG)
                        if wpid != 0:
                            break
                    except ChildProcessError:
                        break
                    time.sleep(0.1)
                else:
                    logger.warning("Child shell did not exit; sending SIGTERM")
                    try:
                        os.kill(pid, signal.SIGTERM)
                        os.waitpid(pid, 0)
                    except ProcessLookupError:
                        pass

    def exec_command(self, command: str, timeout: float = 120.0):
        """Run a command in the live shell and return its output and exit code.

        Raises RuntimeError when the shell is already running another command.
        """
        if not self._exec_lock.acquire(blocking=False):
            raise RuntimeError("shell busy")
        try:
            self.shell_state = "busy"
            self._cap_buf = bytearray()

            baseline_tail = ""
            if self.output_history:
                recent_history = list(self.output_history)[-50:]
                raw_hist = b"".join(recent_history)
                clean_hist = _clean_output(raw_hist)
                hist_lines = [line for line in clean_hist.split("\n") if line.strip()]
                if hist_lines:
                    baseline_tail = hist_lines[-1].strip()

            os.write(self.master_fd, command.encode() + b"\n")

            start = time.time()
            last_len = 0
            silence_start = time.time()
            while time.time() - start < timeout:
                time.sleep(0.1)
                curr_len = len(self._cap_buf)
                if curr_len != last_len:
                    silence_start = time.time()
                    last_len = curr_len
                elif time.time() - silence_start > 0.5:
                    break

            raw = bytes(self._cap_buf)
            nl = raw.find(b"\n")
            body = raw[nl + 1 :] if nl != -1 else raw
            self.shell_state = "idle"
            clean_out = _clean_output(body)

            lines = clean_out.split("\n")
            while lines and not lines[-1].strip():
                lines.pop()

            if lines and baseline_tail and lines[-1].strip() == baseline_tail:
                lines.pop()

            return "\n".join(lines).strip(), 0
        finally:
            self._cap_buf = None
            self._exec_lock.release()

    def _io_loop(self):
        """Relay keyboard input and shell output."""
        while True:
            if self._interrupted:
                logger.info("Interrupted by signal")
                break

            r_fds = [sys.stdin.fileno(), self.master_fd]

            try:
                ready_r, _, _ = select.select(r_fds, [], [], 0.1)
            except InterruptedError:
                continue

            if not ready_r:
                self.indicator.tick(self._effective_cursor_state())

            if self._interrupted:
                logger.info("Interrupted by signal")
                break

            pending = self.indicator.take_pending_input()
            if pending:
                data = pending
            elif sys.stdin.fileno() in ready_r:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                if not data:
                    logger.info("EOF on stdin (Ctrl+D)")
                    os.write(self.master_fd, b"\x04")
                    break
            else:
                data = b""

            if data and any(t in data for t in self.triggers):
                for t in self.triggers:
                    data = data.replace(t, b"")
                self._toggle_listening()

            if data:
                self._user_started_typing()
                os.write(self.master_fd, data)

            if self.master_fd in ready_r:
                try:
                    data = os.read(self.master_fd, 10240)
                except OSError:
                    break

                if not data:
                    break

                self.output_history.append(data)
                if self._cap_buf is not None:
                    self._cap_buf.extend(data)
                self._track_pty_controls(data)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
