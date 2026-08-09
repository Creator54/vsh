import collections
import fcntl
import json
import os
import pty
import queue
import re
import select
import signal
import struct
import sys
import termios
import threading
import tty
from dataclasses import dataclass

from vsh.core.config import VshConfig
from vsh.core.logger import logger
from vsh.core.mic_state import PipeWireMicMonitor
from vsh.core.voice_indicator import CURSOR_RESET as CURSOR_RESET
from vsh.core.voice_indicator import VoiceIndicator
from vsh.core.voice_input import VoiceInputThread, VoiceState

_ANSI = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_][0-?]*[ -/]*[@-~]")
_OSC133 = re.compile(rb"\x1b\]133;([ABCD])(?:;[^\x07\x1b]*)?(?:\x07|\x1b\\)")
_VSH_FISH_PROMPT = b"\x1b]777;vsh-prompt\x07"
_FISH_PROMPT_INIT = (
    "functions --erase __vsh_saved_fish_prompt; "
    "if functions --copy fish_prompt __vsh_saved_fish_prompt; "
    "function fish_prompt; __vsh_saved_fish_prompt $argv; "
    "if not contains -- --final-rendering $argv; printf '\\e]777;vsh-prompt\\a'; end; end; "
    "end"
)


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
        server_port: int | None = None,
    ):
        self.config = config
        self.thinker = thinker
        self.verbose = verbose
        self.tts_provider = tts_provider
        self.voice_handler = voice_handler
        self.server_port = server_port
        self.http_server = None

        if self.config.shell.response_bridge:
            logger.warning("response_bridge is deprecated and ignored; replies now use the PTY directly")

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
        self.inner_shell_args = config.shell.inner_shell_args or [self.inner_shell]

        self.master_fd = None
        self.stt_queue: queue.Queue[str | None] = queue.Queue()
        self._terminal_lock = threading.RLock()
        self._mic_monitor_lock = threading.Lock()
        self.mic_monitor = None
        self.voice_thread = VoiceInputThread(
            self.stt_queue,
            config=self.config,
            device_index=self.config.stt.device_index,
            verbose=self.verbose,
            vad_threshold=self.config.stt.vad_threshold,
            vad_silence_limit=self.config.stt.vad_silence_limit,
            volume_callback=self._volume_callback,
            state_callback=self._set_cursor_state,
            capture_started_callback=self._start_mic_monitor,
            capture_stopped_callback=self._stop_mic_monitor,
        )
        self.pipeline_thread = None
        self.background_thread = None
        self.old_tty_attrs = None
        self._interrupted = False
        self._exiting = False
        self._tts_lock = threading.Lock()
        self._input_lock = threading.Lock()
        self._input_revision = 0
        self._voice_input_revision = 0
        self._prompt_markers_seen = False
        self._prompt_ready = False
        self._prompt_input_revision = 0
        self._prompt_control_tail = b""
        self.indicator = VoiceIndicator(config.shell.overlay_mode, config.stt.vad_threshold, self._terminal_lock)
        self._current_cursor_state: VoiceState | None = None
        self._system_mic_muted: bool | None = None

        self.shell_name = os.path.basename(self.inner_shell)
        self.shell_pid = None
        self.shell_state = "starting"
        self._history_lock = threading.Lock()
        self.output_history: collections.deque[bytes] = collections.deque(maxlen=2000)

    def _pipeline_worker(self):
        """Process voice input without blocking the shell."""
        while True:
            transcript = self.stt_queue.get()
            if transcript is None:
                break
            try:
                if self._exiting:
                    continue
                expected_revision = self._voice_input_revision
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
                        self._dispatch_response(processor.ask(prompt), expected_revision)
                    except Exception as e:
                        self._publish_reply(f"vsh: {e}", "", expected_revision)
                        self._set_cursor_state(self._resting_voice_state())
                else:
                    self._inject_command(transcript, expected_revision)
                    self._set_cursor_state(self._resting_voice_state())
            finally:
                self.stt_queue.task_done()

    def _load_tts(self):
        if self.tts_provider is not None:
            return self.tts_provider
        if self.config.tts.provider in ("", "none"):
            return None
        with self._tts_lock:
            if self.tts_provider is None:
                from vsh.providers import resolve_tts

                self.tts_provider = resolve_tts(self.config)
                if self.tts_provider is None:
                    logger.warning(f"Unknown or failed TTS provider: {self.config.tts.provider}")
        return self.tts_provider

    def _speak(self, text: str) -> bool:
        """Play the reply before running its command."""
        try:
            from vsh.core.audio import play_audio, synthesize_pcm16

            provider = self._load_tts()
            if provider is None:
                return False
            data, rate = synthesize_pcm16(provider, text)
            play_audio(data, rate, device_index=self.config.tts.device_index)
            return True
        except Exception as e:
            logger.error(f"TTS Error: {e}")
            return False

    def _dispatch_response(self, raw: str, expected_revision: int | None = None):
        """Speak or print the reply, then run its command."""
        if self._exiting:
            return
        reply = parse_voice_reply(raw)
        visible_speech = reply.speech
        if reply.speech and (self.tts_provider is not None or self.config.tts.provider not in ("", "none")):
            self._set_cursor_state(VoiceState.SPEAKING)
            spoke = self._speak(reply.speech)
            self.voice_thread.suppress_input(0.3)
            if spoke:
                visible_speech = ""
        if reply.command:
            self._set_cursor_state(VoiceState.TYPING)
        if visible_speech or reply.command:
            if expected_revision is None:
                self._publish_reply(visible_speech, reply.command)
            else:
                self._publish_reply(visible_speech, reply.command, expected_revision)
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
        with self._terminal_lock:
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
        with self._terminal_lock:
            if self._exiting:
                return
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
        with self._terminal_lock:
            if self._exiting:
                return
            state = VoiceState(state) if state is not None else None
            if state == VoiceState.TRANSCRIBING:
                with self._input_lock:
                    self._voice_input_revision = self._input_revision
            if state in (VoiceState.TRANSCRIBING, VoiceState.THINKING, VoiceState.TYPING, VoiceState.SPEAKING):
                if hasattr(self, "voice_thread"):
                    self.voice_thread.set_processing(True)
            elif state in (None, VoiceState.MUTED, VoiceState.IDLE):
                if hasattr(self, "voice_thread"):
                    self.voice_thread.set_processing(False)
            self._current_cursor_state = state
            self.indicator.render(self._effective_cursor_state())

    def _set_system_mic_muted(self, muted: bool | None):
        with self._terminal_lock:
            if self._exiting:
                return
            if hasattr(self, "voice_thread"):
                self.voice_thread.set_system_mic_muted(muted)
            self._system_mic_muted = muted
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
        with self._terminal_lock:
            if not self._exiting:
                self.indicator.user_started_typing()

    def _track_pty_controls(self, data: bytes):
        stream = self._prompt_control_tail + data
        events = [(match.start(), match.end(), match.group(1)) for match in _OSC133.finditer(stream)]
        events.extend((match.start(), match.end(), b"V") for match in re.finditer(re.escape(_VSH_FISH_PROMPT), stream))
        events.sort()
        if events:
            with self._input_lock:
                for _, _, event in events:
                    self._prompt_markers_seen = True
                    if event == b"V" or (event == b"A" and self.shell_name != "fish"):
                        self._prompt_ready = True
                        self._prompt_input_revision = self._input_revision
                    elif event in (b"C", b"D"):
                        self._prompt_ready = False

        remaining = stream[max((end for _, end, _ in events), default=0) :]
        prefixes = (b"\x1b]133;", _VSH_FISH_PROMPT[:-1])
        starts = [remaining.rfind(prefix) for prefix in prefixes]
        starts = [start for start in starts if start >= 0]
        if starts:
            self._prompt_control_tail = remaining[min(starts) :][-1024:]
        else:
            overlap = next(
                (
                    size
                    for size in range(min(max(map(len, prefixes)) - 1, len(remaining)), 0, -1)
                    if any(remaining.endswith(prefix[:size]) for prefix in prefixes if size < len(prefix))
                ),
                0,
            )
            self._prompt_control_tail = remaining[-overlap:] if overlap else b""
        self.indicator.track_output(data)

    def _shell_exec_args(self) -> list[str]:
        if self.shell_name != "fish":
            return self.inner_shell_args
        args = list(self.inner_shell_args)
        value_options = {
            "-C",
            "--init-command",
            "-d",
            "--debug",
            "-o",
            "--debug-output",
            "-p",
            "--profile",
            "--profile-startup",
            "-f",
            "--features",
        }
        insertion = len(args)
        index = 1
        while index < len(args):
            argument = args[index]
            if argument in ("-c", "--command", "--") or not argument.startswith("-"):
                insertion = index
                break
            index += 2 if argument in value_options else 1
        return [*args[:insertion], "--init-command", _FISH_PROMPT_INIT, *args[insertion:]]

    def _notify(self, msg: str, color="36"):
        """Verbose notification for startup and toggle diagnostics."""
        with self._terminal_lock:
            if not self.verbose or self._exiting:
                return
            sys.stdout.buffer.write(f"\r\n\033[{color}m◆\033[0m {msg}\r\n".encode())
            sys.stdout.buffer.flush()

    def _start_mic_monitor(self):
        with self._mic_monitor_lock:
            if self._exiting:
                return
            if self.mic_monitor is not None:
                if self.mic_monitor.is_alive():
                    return
                self.mic_monitor = None
            monitor = PipeWireMicMonitor(lambda muted: self._publish_mic_monitor_state(monitor, muted))
            self.mic_monitor = monitor
            try:
                monitor.start()
            except Exception:
                if self.mic_monitor is monitor:
                    self.mic_monitor = None
                raise

    def _publish_mic_monitor_state(self, monitor, muted: bool | None):
        with self._mic_monitor_lock:
            if self.mic_monitor is monitor:
                self._set_system_mic_muted(muted)

    def _stop_mic_monitor(self):
        with self._mic_monitor_lock:
            monitor = self.mic_monitor
        if monitor is None:
            return
        monitor.stop()
        monitor.join(timeout=2)
        with self._mic_monitor_lock:
            if self.mic_monitor is monitor:
                self.mic_monitor = None

    def _toggle_listening(self):
        if self._exiting:
            return
        try:
            enabled = self.voice_thread.toggle_listening()
        except OSError as error:
            logger.error(f"Failed to acquire microphone: {error}")
            return
        if enabled:
            self.voice_thread.suppress_input(0.6)
        if self.verbose:
            m = (
                f"Listening · press {self.config.keybinds.toggle_listen} or Ctrl+G to pause"
                if enabled
                else "Listening paused"
            )
            c = "1;35" if enabled else "36"
            self._notify(m, color=c)
        if enabled:
            with self._terminal_lock:
                if not self._exiting:
                    sys.stdout.buffer.write(b"\a")
                    sys.stdout.buffer.flush()

    def _inject_command(self, cmd: str, expected_revision: int | None = None) -> bool:
        """Send text directly to the shell."""
        if not cmd or self._exiting:
            return False

        if getattr(self.config.shell, "auto_submit", False) and not cmd.endswith("\n"):
            cmd += "\n"

        if self.master_fd is None:
            logger.error("Failed to write to PTY: master_fd is None")
            return False

        with self._input_lock:
            if expected_revision is not None:
                try:
                    owns_pty = os.tcgetpgrp(self.master_fd) == self.shell_pid
                except OSError:
                    owns_pty = False
                prompt_safe = (
                    self._prompt_markers_seen
                    and owns_pty
                    and self._prompt_ready
                    and self._prompt_input_revision == self._input_revision
                )
                if expected_revision != self._input_revision or not prompt_safe:
                    logger.warning("Skipped voice command because terminal input or ownership changed")
                    skipped = True
                else:
                    skipped = False
            else:
                skipped = False
            if not skipped:
                try:
                    os.write(self.master_fd, cmd.encode())
                    return True
                except OSError as e:
                    logger.error(f"Failed to write to PTY: {e}")
                    return False

        with self._terminal_lock:
            if not self._exiting:
                sys.stdout.write(f"\r\nvsh: command not injected: {json.dumps(cmd.rstrip(), ensure_ascii=False)}\r\n")
                sys.stdout.flush()
        return False

    def _publish_reply(self, speech: str, command: str, expected_revision: int | None = None):
        """Publish speech and command as one ordered terminal update."""
        with self._terminal_lock:
            if self._exiting:
                return
            if speech:
                sys.stdout.write(f"\r\n{speech.rstrip()}\r\n")
                sys.stdout.flush()
            if command:
                if expected_revision is None:
                    self._inject_command(command)
                else:
                    self._inject_command(command, expected_revision)

    def _cancel_processors(self):
        for processor in (self.voice_handler, self.thinker):
            cancel = getattr(processor, "cancel", None)
            if cancel is not None:
                try:
                    cancel()
                except Exception as error:
                    logger.error(f"Failed to cancel processor during shutdown: {error}")

    def output_snapshot(self) -> bytes:
        with self._history_lock:
            return b"".join(self.output_history)

    def _reap_shell(self):
        pid = self.shell_pid
        if not pid:
            return
        while True:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    return
                break
            except InterruptedError:
                continue
            except (ChildProcessError, OSError):
                return

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

        while True:
            try:
                os.waitpid(pid, 0)
                return
            except InterruptedError:
                continue
            except (ChildProcessError, OSError):
                return

    def _kill_foreground_process_group(self):
        if self.master_fd is None or not self.shell_pid:
            return
        try:
            process_group = os.tcgetpgrp(self.master_fd)
            if process_group == self.shell_pid:
                return
            os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass

    def _start_http_server(self):
        if self.server_port is None or self._exiting:
            return
        try:
            from vsh.core.server import serve

            self.http_server = serve(self, port=self.server_port)
            if self.http_server is not None:
                host, port = self.http_server.server_address
                logger.success(
                    f"HTTP bridge · http://{host}:{port} · Authorization: Bearer {self.http_server.auth_token}"
                )
        except Exception as error:
            logger.error(f"Shell bridge failed: {error}")

    def run(self):
        """Start the shell, background workers, and input/output loop."""
        pid, self.master_fd = pty.fork()

        if pid == 0:
            try:
                os.environ["VSH_ACTIVE"] = "1"
                os.environ["VSH_ACTIVE_TTY"] = os.ttyname(sys.stdin.fileno())
                os.execv(self.inner_shell, self._shell_exec_args())
            except Exception as e:
                sys.stderr.write(f"\033[31m✗\033[0m Failed to start {self.inner_shell} · {e}\n")
                os._exit(1)
        else:
            self.shell_pid = pid
            try:
                signal.signal(signal.SIGINT, self._handle_sigint)
                signal.signal(signal.SIGTERM, self._handle_sigint)
                signal.signal(signal.SIGHUP, self._handle_sigint)
                signal.signal(signal.SIGWINCH, self._handle_sigwinch)
                self._handle_sigwinch(None, None)

                self._setup_terminal()
                self.indicator.select_mode(self.old_tty_attrs is not None)
                self._start_http_server()

                def async_bg_init():
                    if self._exiting:
                        return
                    try:
                        self.voice_thread.start()
                        if self._exiting:
                            self.voice_thread.stop()
                            return
                        self.pipeline_thread = threading.Thread(target=self._pipeline_worker, daemon=True)
                        self.pipeline_thread.start()
                        self.shell_state = "idle"
                        if self.verbose and not self._exiting:
                            self._notify(f"VSH active. Press {self.config.keybinds.toggle_listen} or Ctrl+G to toggle.")
                        if self.config.shell.voice_on_start and not self._exiting:
                            self._toggle_listening()
                    except Exception as error:
                        if not self._exiting:
                            logger.error(f"Background initialization failed: {error}")

                self.background_thread = threading.Thread(target=async_bg_init, daemon=True)
                self.background_thread.start()
                self._io_loop()
            finally:
                self._exiting = True
                if self.background_thread is not None:
                    try:
                        self.background_thread.join()
                    except RuntimeError:
                        pass
                self._cancel_processors()
                if self.http_server is not None:
                    try:
                        self.http_server.shutdown()
                    except OSError:
                        pass
                    try:
                        self.http_server.server_close()
                    except OSError:
                        pass
                    self.http_server = None
                try:
                    self.stt_queue.put_nowait(None)
                except Exception:
                    pass
                try:
                    self.voice_thread.stop()
                except Exception:
                    pass
                try:
                    if self.voice_thread.is_alive():
                        self.voice_thread.join(timeout=3)
                except Exception:
                    pass
                try:
                    self._stop_mic_monitor()
                except Exception:
                    pass
                self._kill_foreground_process_group()
                self._restore_terminal()
                if self.master_fd is not None:
                    try:
                        os.close(self.master_fd)
                    except OSError:
                        pass
                    self.master_fd = None
                self._reap_shell()

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
                with self._input_lock:
                    try:
                        os.write(self.master_fd, data)
                        self._input_revision += 1
                    except OSError:
                        break

            if self.master_fd in ready_r:
                try:
                    data = os.read(self.master_fd, 10240)
                except OSError:
                    break

                if not data:
                    break

                with self._history_lock:
                    self.output_history.append(data)
                self._track_pty_controls(data)
                with self._terminal_lock:
                    if self._exiting:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
