import base64
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
from vsh.core.voice_input import VoiceInputThread

# ANSI escape sequences for cursor control.
CURSOR_RESET = b"\033]112\a\033[0 q"

_GRAPHICS_RESPONSE = rb"\x1b_Gi={image_id}(?:,[^;]*)?;[ -~]*\x1b\\"
_DEVICE_ATTRIBUTES_RESPONSE = re.compile(rb"\x1b\[\?[0-9;]*c")

# Strip ANSI/OSC (incl. trailing partial OSC) from captured command output.
_ANSI = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_][0-?]*[ -/]*[@-~]")


def _strip_ansi(b: bytes) -> bytes:
    return _ANSI.sub(b"", b)


def _strip_unicode(text: str) -> str:
    """Generic unicode cleanup for UI characters (Box drawing, Symbols, Nerd Fonts, Emojis)."""
    return re.sub(r"[\u2300-\u25ff\u2700-\u27bf\ue000-\uf8ff]|[\ud800-\udbff][\udc00-\udfff]", "", text)


@dataclass(frozen=True)
class VoiceReply:
    speech: str = ""
    command: str = ""


def parse_voice_reply(raw: str) -> VoiceReply:
    """Parse the strict voice-shell contract, falling back safely to speech."""
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
        "command_only": "Set speech to an empty string and command to the requested Fish command.",
        "speak_only": "Set speech to a concise response and command to null.",
        "speak_and_command": (
            "Set speech to a concise response. Set command to the Fish command when one should run, otherwise null."
        ),
    }
    rule = rules.get(mode, rules["speak_and_command"])
    return (
        "Return exactly one JSON object and no markdown: "
        '{"speech":"brief response","command":null}. '
        "The command value must be either a Fish command string or null. "
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

        # Precompute keybind triggers to avoid loop overhead
        k = self.config.keybinds.toggle_listen.lower()
        self.triggers = []

        # Load custom triggers from config if available
        if hasattr(self.config.keybinds, "toggle_listen_triggers") and self.config.keybinds.toggle_listen_triggers:
            for t in self.config.keybinds.toggle_listen_triggers:
                try:
                    self.triggers.append(bytes.fromhex(t))
                except Exception:
                    pass

        # Fallback for old configs
        if not self.triggers:
            if k in ("ctrl+\\", "ctrl+\\\\"):
                self.triggers = [b"\x1c", b"\x1b[92;5u", b"\x1b[92;133u", b"\x1b[28;5u", b"\x1b[28;133u"]
            elif k == "ctrl+g":
                self.triggers = [b"\x07", b"\x1b[103;5u", b"\x1b[103;133u"]
            else:
                self.triggers = [b"\x1c", b"\x1b[92;5u", b"\x1b[92;133u", b"\x1b[28;5u", b"\x1b[28;133u"]  # default

        # Keep Ctrl+G and Ctrl+] available alongside configured triggers.
        fallback_triggers = [b"\x07", b"\x1d", b"\x1b[103;5u", b"\x1b[103;133u"]
        for t in fallback_triggers:
            if t not in self.triggers:
                self.triggers.append(t)

        # Determine which shell to run
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
        self.pipeline_thread = None
        self.is_listening = False
        self.old_tty_attrs = None
        self._interrupted = False
        self.cols = 80
        self.rows = 24
        self._last_energy = 0
        self._anim_frame = 0
        self._visual_mode = "none"
        self._pending_input = b""
        self._image_id = secrets.randbelow(2**31 - 1) + 1
        self._placement_id = 1
        self._graphics_visible = False
        self._last_graphics_signature = None
        self._last_graphics_render = 0.0
        self._typing_until = 0.0
        self._alternate_screen = False
        self._pty_control_tail = b""

        # Shell identity and live execution state exposed over HTTP.
        self.shell_name = os.path.basename(self.inner_shell)
        self.shell_pid = None
        self.shell_state = "starting"  # starting|idle|busy
        self._exec_lock = threading.Lock()
        self._cap_buf = None  # bytes buffer while capturing
        self._cap_done = threading.Event()
        self._cap_exit = None

        self.input_history = collections.deque(maxlen=2000)
        self.output_history = collections.deque(maxlen=2000)

    def _pipeline_worker(self):
        """Background worker to handle thinking and coordination without blocking the shell."""
        while True:
            transcript = self.stt_queue.get()
            if transcript is None:
                break

            self._current_transcript = transcript

            if self.verbose:
                logger.info(f"Processing: {transcript}")

            processor = self.voice_handler or self.thinker
            if processor:
                self._set_cursor_state("thinking")
                try:
                    prompt = transcript
                    if not self.voice_handler:
                        mode = getattr(self.config.llm, "output_mode", "speak_and_command")
                        prompt = _voice_prompt(transcript, mode)
                    self._dispatch_response(processor.ask(prompt))
                except Exception as e:
                    self._publish_reply(f"vsh: {e}", "")
                    self._set_cursor_state("idle")
            else:
                # Direct injection
                self._inject_command(transcript)
                self._set_cursor_state("idle")

            self.stt_queue.task_done()

    def _speak(self, text: str) -> bool:
        """Play speech synchronously in the pipeline thread so commands follow it."""
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
        """Speak or print response text first, then hand the command to Fish."""
        reply = parse_voice_reply(raw)
        visible_speech = reply.speech
        if reply.speech and self.tts_provider:
            self._set_cursor_state("speaking")
            if self._speak(reply.speech):
                visible_speech = ""
        if reply.command:
            self._set_cursor_state("typing")
        if visible_speech or reply.command:
            self._publish_reply(visible_speech, reply.command)
        self._set_cursor_state("idle")

    def _setup_terminal(self):
        """Save terminal state and enter raw mode (skip if no TTY, e.g. --serve)."""
        if not os.isatty(sys.stdin.fileno()):
            self.old_tty_attrs = None
            return
        self.old_tty_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def _probe_graphics_support(self, timeout: float = 0.25) -> bool:
        """Ask the outer terminal whether it supports the Kitty graphics protocol."""
        if self.old_tty_attrs is None:
            return False

        image_id = str(self._image_id).encode()
        query = b"\033_Gi=" + image_id + b",s=1,v=1,a=q,t=d,f=24;AAAA\033\\\033[c"
        sys.stdout.buffer.write(self._terminal_sequence(query))
        sys.stdout.buffer.flush()

        response = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([sys.stdin.fileno()], [], [], remaining)
            if not ready:
                break
            chunk = os.read(sys.stdin.fileno(), 1024)
            if not chunk:
                break
            response.extend(chunk)
            if _DEVICE_ATTRIBUTES_RESPONSE.search(response):
                break

        graphics_response = re.compile(_GRAPHICS_RESPONSE.replace(b"{image_id}", image_id))
        graphics_match = graphics_response.search(response)
        attributes_match = _DEVICE_ATTRIBUTES_RESPONSE.search(response)
        supported = bool(graphics_match and (not attributes_match or graphics_match.start() < attributes_match.start()))

        pending = graphics_response.sub(b"", bytes(response))
        pending = _DEVICE_ATTRIBUTES_RESPONSE.sub(b"", pending)
        self._pending_input += pending
        return supported

    @staticmethod
    def _terminal_sequence(sequence: bytes) -> bytes:
        """Pass Kitty control sequences through tmux when passthrough is enabled."""
        if os.environ.get("TMUX"):
            # tmux DCS passthrough requires embedded ESC bytes to be doubled.
            return b"\033Ptmux;" + sequence.replace(b"\033", b"\033\033") + b"\033\\"
        return sequence

    def _select_visual_mode(self):
        """Resolve auto/kitty/none to the renderer used for this terminal session."""
        configured = getattr(self.config.shell, "overlay_mode", "auto").lower()
        if configured not in ("auto", "kitty", "none"):
            logger.warning("Unknown overlay mode {!r}; using auto", configured)
            configured = "auto"
        if configured == "none" or self.old_tty_attrs is None:
            self._visual_mode = "none"
        elif configured == "kitty" or self._probe_graphics_support():
            self._visual_mode = "graphics"
        else:
            self._visual_mode = "cursor"

    def _restore_terminal(self):
        """Remove VSH visuals and restore the original terminal state."""
        if not self.old_tty_attrs:
            return
        try:
            if self._visual_mode == "graphics":
                self._delete_graphics_badge()
            elif self._visual_mode == "cursor":
                sys.stdout.buffer.write(CURSOR_RESET)
                sys.stdout.buffer.flush()
        except (OSError, ValueError, termios.error):
            pass
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, self.old_tty_attrs)
        except (OSError, ValueError, termios.error):
            pass

    def _handle_sigint(self, signum, frame):
        """Graceful interrupt flag for Ctrl+C."""
        self._interrupted = True

    def _handle_sigwinch(self, signum, frame):
        """Propagate window resize to the PTY."""
        if self.master_fd is None:
            return

        # Get terminal size from stdout
        try:
            buf = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"0000")
            rows, cols = struct.unpack("hh", buf)
            self.rows, self.cols = rows, cols

            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), cols, 0, 0))
        except Exception:
            pass

    def _volume_callback(self, energy: int, threshold: int):
        self._last_energy = energy
        self._last_threshold = threshold
        if not getattr(self, "is_listening", False):
            return

        pipeline = getattr(self, "_pipeline_state", None)

        if pipeline:
            state = pipeline
        elif energy > threshold:
            state = "listening_active"
        else:
            state = "listening_idle"

        current = getattr(self, "_current_cursor_state", "idle")
        if current != state:
            self._set_cursor_state(state)

    def _set_cursor_state(self, state: str, text: str = None):
        """Update pipeline state and its visual indicator."""

        if state in ("transcribing", "thinking", "typing", "speaking"):
            self._pipeline_state = state
            if hasattr(self, "voice_thread"):
                self.voice_thread.is_processing = True
        elif state == "idle":
            self._pipeline_state = None
            if hasattr(self, "voice_thread"):
                self.voice_thread.is_processing = False

        self._current_cursor_state = state
        if self._visual_mode == "cursor":
            self._render_cursor_state(state)
        elif self._visual_mode == "graphics":
            self._render_graphics_badge()

    def _user_started_typing(self):
        self._typing_until = time.monotonic() + 0.6
        if self._visual_mode == "graphics":
            self._delete_graphics_badge()
        elif self._visual_mode == "cursor":
            self._render_cursor_state("typing")

    def _track_pty_controls(self, data: bytes):
        """Track alternate-screen transitions without parsing the whole VT stream."""
        stream = self._pty_control_tail + data
        for match in re.finditer(rb"\x1b\[\?(1049|1047|47)(h|l)", stream):
            entering = match.group(2) == b"h"
            if entering != self._alternate_screen:
                self._alternate_screen = entering
                if entering and self._visual_mode == "graphics":
                    self._delete_graphics_badge()
        self._pty_control_tail = stream[-16:]

    def _notify(self, msg: str, color="36"):
        """Verbose notification for startup and toggle diagnostics."""
        if self.verbose:
            sys.stdout.buffer.write(f"\r\n\033[{color}m[vsh]\033[0m {msg}\r\n".encode())
            sys.stdout.buffer.flush()

    def _toggle_listening(self):
        self.is_listening = self.voice_thread.toggle_listening()
        if self.verbose:
            m = (
                f"LISTENING (Press {self.config.keybinds.toggle_listen} or Ctrl+G to pause)"
                if self.is_listening
                else "STOPPED"
            )
            c = "1;35" if self.is_listening else "36"
            self._notify(m, color=c)
        if self.is_listening:
            sys.stdout.buffer.write(b"\a")
            self._set_cursor_state("listening_idle")
        else:
            self._set_cursor_state("idle")

    def _inject_command(self, cmd: str):
        """Inject text directly into the PTY."""
        if not cmd:
            return

        # Append newline if auto_submit is enabled (e.g. for conversational LLM chat CLI)
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
        """Main entry point: fork PTY, start threads, and multiplex I/O."""
        # Set recursion guard so inner shell doesn't spawn vsh again
        os.environ["VSH_ACTIVE"] = "1"

        # Fork the PTY
        pid, self.master_fd = pty.fork()

        if pid == 0:
            # Child process: execute the shell
            try:
                os.execv(self.inner_shell, self.inner_shell_args)
            except Exception as e:
                sys.stderr.write(f"[vsh] Failed to start shell: {self.inner_shell}: {e}\n")
                os._exit(1)
        else:
            # Parent process: act as proxy
            self.shell_pid = pid

            # Setup signal handlers
            signal.signal(signal.SIGINT, self._handle_sigint)
            signal.signal(signal.SIGTERM, self._handle_sigint)
            signal.signal(signal.SIGHUP, self._handle_sigint)
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
            self._handle_sigwinch(None, None)

            # Start background STT thread
            self.voice_thread.start()

            # The pipeline worker handles LLM/Thinking
            self.pipeline_thread = threading.Thread(target=self._pipeline_worker, daemon=True)
            self.pipeline_thread.start()

            # Setup raw mode
            self._setup_terminal()
            self._select_visual_mode()

            # Defer UI updates slightly so the inner shell doesn't overwrite them
            def show_startup_ui():
                time.sleep(0.5)
                self.shell_state = "idle"
                if self.verbose:
                    self._notify(f"VSH active. Press {self.config.keybinds.toggle_listen} or Ctrl+G to toggle.")
                if self.config.shell.voice_on_start:
                    # _toggle_listening() renders the new state immediately; the io_loop's
                    # 0.1s select() already wakes on its own, so we must NOT inject a byte
                    # into the inner shell (it would arrive as Ctrl+Space/Null input).
                    self._toggle_listening()

            threading.Thread(target=show_startup_ui, daemon=True).start()

            try:
                self._io_loop()
            finally:
                # Cleanup
                self._restore_terminal()
                self.voice_thread.stop()
                self.voice_thread.join(timeout=2.0)
                self.stt_queue.put(None)  # Signal pipeline to stop
                # Graceful child shutdown with timeout
                for _ in range(50):  # 5 seconds
                    try:
                        wpid, status = os.waitpid(pid, os.WNOHANG)
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

    def _render_cursor_state(self, state: str):
        """Fallback indicator that never writes into terminal cells."""
        if not self.is_listening:
            sequence = CURSOR_RESET
        else:
            state = "listening_idle" if state == "idle" else state
            sequence = {
                "listening_idle": b"\033]12;#00d2ff\a\033[6 q",
                "listening_active": b"\033]12;#ff00b4\a\033[1 q",
                "transcribing": b"\033]12;#00d2ff\a\033[3 q",
                "thinking": b"\033]12;#ffae00\a\033[4 q",
                "typing": b"\033]12;#32dc64\a\033[6 q",
                "speaking": b"\033]12;#32dc64\a\033[5 q",
            }.get(state, CURSOR_RESET)
        sys.stdout.buffer.write(sequence)
        sys.stdout.buffer.flush()

    def _icon_pixels(self, state: str, phase: int) -> bytes:
        """Build a tiny transparent RGBA icon without an image dependency."""
        colors = {
            "listening_idle": (0, 210, 255),
            "listening_active": (255, 0, 180),
            "transcribing": (0, 210, 255),
            "thinking": (255, 174, 0),
            "typing": (50, 220, 100),
            "speaking": (50, 220, 100),
        }
        state = "listening_idle" if state == "idle" else state
        color = colors.get(state, colors["listening_idle"])
        pixels = bytearray(16 * 16 * 4)

        def put(x: int, y: int, alpha: int = 255):
            if 0 <= x < 16 and 0 <= y < 16:
                offset = (y * 16 + x) * 4
                pixels[offset : offset + 4] = (*color, alpha)

        def hline(x1: int, x2: int, y: int, alpha: int = 255):
            for x in range(x1, x2 + 1):
                put(x, y, alpha)

        def vline(x: int, y1: int, y2: int, alpha: int = 255):
            for y in range(y1, y2 + 1):
                put(x, y, alpha)

        if state == "listening_idle":
            # Soft breathing orb, matching the old `•` / `(•)` / `◖•◗` HUD.
            put(7, 7)
            put(8, 7)
            put(7, 8)
            put(8, 8)
            rings = (
                ((5, 7), (10, 7), (7, 5), (7, 10)),
                ((4, 7), (11, 7), (7, 4), (7, 11), (5, 5), (10, 5), (5, 10), (10, 10)),
                ((3, 7), (12, 7), (7, 3), (7, 12), (4, 4), (11, 4), (4, 11), (11, 11)),
                ((2, 7), (13, 7), (7, 2), (7, 13), (3, 3), (12, 3), (3, 12), (12, 12)),
            )
            for x, y in rings[phase % len(rings)]:
                put(x, y, 170 if phase < 3 else 120)
        elif state == "listening_active":
            # Responsive audio bars, replacing the literal microphone glyph.
            threshold = max(1, getattr(self, "_last_threshold", self.config.stt.vad_threshold))
            energy = getattr(self, "_last_energy", 0)
            level = 1 if energy <= threshold * 2.5 else 2 if energy <= threshold * 5 else 3
            heights = (3, 6, 9, 6, 3)
            for index, x in enumerate(range(3, 13, 2)):
                height = min(9, heights[(index + phase) % len(heights)] + level)
                top = 8 - height // 2
                vline(x, top, top + height - 1)
        elif state == "transcribing":
            heights = (2, 5, 8, 3, 6, 10, 4, 7, 3, 9, 5, 2)
            for index, x in enumerate(range(2, 14)):
                height = heights[(index + phase) % len(heights)]
                top = 8 - height // 2
                vline(x, top, top + height - 1)
        elif state == "thinking":
            for index, x in enumerate((4, 8, 12)):
                alpha = 255 if index == phase % 3 else 100
                for px in (x - 1, x):
                    for py in (7, 8):
                        put(px, py, alpha)
        elif state == "typing":
            hline(2, 13, 4)
            hline(2, 13, 12)
            vline(2, 5, 11)
            vline(13, 5, 11)
            for x in (4, 7, 10):
                put(x, 7)
            for x in (4, 6, 8, 10):
                put(x, 9)
            hline(5, 10, 11)
            if phase % 2:
                vline(11, 8, 10)
        elif state == "speaking":
            vline(3, 7, 9)
            vline(4, 6, 10)
            vline(5, 5, 11)
            vline(6, 4, 12)
            arcs = (
                ((9, 6), (10, 7), (10, 9), (9, 10)),
                ((11, 4), (12, 5), (13, 7), (13, 9), (12, 11), (11, 12)),
            )
            for arc in arcs[: 1 + phase % 2]:
                for x, y in arc:
                    put(x, y)

        return bytes(pixels)

    def _delete_graphics_badge(self):
        if not self._graphics_visible:
            return
        sequence = f"\033_Ga=d,d=I,i={self._image_id},p={self._placement_id},q=2;\033\\".encode()
        sys.stdout.buffer.write(self._terminal_sequence(sequence) + b"\033[?25h")
        sys.stdout.buffer.flush()
        self._graphics_visible = False
        self._last_graphics_signature = None

    def _render_graphics_badge(self):
        """Render the animated two-cell state cursor at the current terminal cursor."""
        if (
            not self.is_listening
            or self.cols < 2
            or self._alternate_screen
            or time.monotonic() < self._typing_until
        ):
            self._delete_graphics_badge()
            return

        state = getattr(self, "_current_cursor_state", "idle")
        phase = self._anim_frame
        self._anim_frame += 1
        animation_phase = (
            phase % 12
            if state == "transcribing"
            else phase % 3
            if state == "thinking"
            else phase % 4
            if state in ("listening_idle", "listening_active")
            else phase % 2
        )
        pixels = self._icon_pixels(state, animation_phase)
        payload = base64.standard_b64encode(pixels)
        control = (
            f"a=T,f=32,s=16,v=16,i={self._image_id},p={self._placement_id},"
            "c=2,r=1,C=1,z=1,q=2"
        ).encode()
        # Paint the badge at the top-right edge as a non-blocking HUD.  The
        # shell cursor is saved/restored so this never inserts text or steals
        # the user's input position.
        row = 1
        col = max(1, self.cols - 1)
        sequence = (
            f"\033[s\033[{row};{col}H\033[?25l".encode()
            + b"\033_G"
            + control
            + b";"
            + payload
            + b"\033\\\033[u"
        )
        sys.stdout.buffer.write(self._terminal_sequence(sequence))
        sys.stdout.buffer.flush()
        self._graphics_visible = True
        self._last_graphics_render = time.monotonic()

    def exec_command(self, command: str, timeout: float = 120.0):
        """Inject a command into the live shell, capture its output + exit code.

        Returns (output_str, exit_code). Raises RuntimeError if the shell is
        busy (user or another exec mid-command).
        """
        if not self._exec_lock.acquire(blocking=False):
            raise RuntimeError("shell busy")
        try:
            self.shell_state = "busy"
            self._cap_buf = bytearray()

            # Snapshot the baseline prompt (last non-empty line of current terminal state)
            baseline_tail = ""
            if self.output_history:
                # deque does not support slicing, convert to list first
                recent_history = list(self.output_history)[-50:]
                raw_hist = b"".join(recent_history)
                clean_hist = _strip_unicode(_strip_ansi(raw_hist).decode("utf-8", "replace"))
                hist_lines = [line for line in clean_hist.split("\n") if line.strip()]
                if hist_lines:
                    baseline_tail = hist_lines[-1].strip()

            os.write(self.master_fd, command.encode() + b"\n")

            # Wait for silence to assume command completion
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
            clean_out = _strip_unicode(_strip_ansi(body).decode("utf-8", "replace"))

            lines = clean_out.split("\n")
            while lines and not lines[-1].strip():
                lines.pop()

            # Dynamic Cleanup: If the bottom line of output exactly matches the baseline prompt tail, drop it!
            if lines and baseline_tail and lines[-1].strip() == baseline_tail:
                lines.pop()

            return "\n".join(lines).strip(), 0
        finally:
            self._cap_buf = None
            self._exec_lock.release()

    def _io_loop(self):
        """The main select() loop that multiplexes stdin and PTY. Non-blocking."""
        while True:
            if self._interrupted:
                logger.info("Interrupted by signal")
                break

            # Read inputs: stdin and the PTY master
            r_fds = [sys.stdin.fileno(), self.master_fd]

            try:
                # Main multiplexer: No thinking or audio work here!
                ready_r, _, _ = select.select(r_fds, [], [], 0.1)
            except InterruptedError:
                continue

            if not ready_r:
                if self._visual_mode == "graphics":
                    self._render_graphics_badge()
                elif self._visual_mode == "cursor" and self._typing_until:
                    if time.monotonic() >= self._typing_until:
                        self._typing_until = 0.0
                        self._render_cursor_state(getattr(self, "_current_cursor_state", "idle"))

            if self._interrupted:
                logger.info("Interrupted by signal")
                break

            if self._pending_input:
                data = self._pending_input
                self._pending_input = b""
            elif sys.stdin.fileno() in ready_r:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                if not data:
                    logger.info("EOF on stdin (Ctrl+D)")
                    # Forward EOT to child so the shell can process it gracefully
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
                self.input_history.append(data)
                os.write(self.master_fd, data)

            # 2. Process PTY output
            if self.master_fd in ready_r:
                try:
                    data = os.read(self.master_fd, 10240)
                except OSError:
                    break  # Child exited

                if not data:
                    break

                self.output_history.append(data)
                if self._cap_buf is not None:
                    self._cap_buf.extend(data)
                self._track_pty_controls(data)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
