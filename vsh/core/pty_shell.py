import fcntl
import os
import pty
import queue
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty

from loguru import logger

from vsh.core.config import VshConfig
from vsh.core.voice_input import VoiceInputThread

# ANSI escape sequences for cursor control and terminal title
CURSOR_DEFAULT = b"\033]112\a\033[0 q\033]0;vsh\a"


class PtyShell:
    def __init__(self, config: VshConfig, thinker=None, verbose: bool = False, tts_provider=None):
        self.config = config
        self.thinker = thinker
        self.verbose = verbose
        self.tts_provider = tts_provider

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

        # Always provide Ctrl+G as a fallback (5u = Ctrl, 133u = Ctrl+NumLock)
        fallback_triggers = [b"\x07", b"\x1b[103;5u", b"\x1b[103;133u"]
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

        self.master_fd = None
        self.stt_queue = queue.Queue()
        self.tts_queue = queue.Queue()
        self.voice_thread = VoiceInputThread(
            self.stt_queue,
            provider_name=self.config.stt.provider,
            device_index=self.config.stt.device_index,
            verbose=self.verbose,
            vad_threshold=self.config.stt.vad_threshold,
            vad_silence_limit=self.config.stt.vad_silence_limit,
            volume_callback=self._volume_callback,
            state_callback=self._set_cursor_state,
        )
        self.pipeline_thread = None
        self.tts_worker = None
        self.is_listening = False
        self.old_tty_attrs = None
        self._interrupted = False
        self.cols = 80
        self.rows = 24
        self._last_energy = 0
        self._anim_frame = 0

    def _pipeline_worker(self):
        """Background worker to handle thinking and coordination without blocking the shell."""
        while True:
            transcript = self.stt_queue.get()
            if transcript is None:
                break

            self._current_transcript = transcript

            if self.verbose:
                logger.info(f"Processing: {transcript}")

            if self.thinker:
                self._set_cursor_state("thinking")
                try:
                    mode = getattr(self.config.llm, "output_mode", "speak_and_command")
                    if mode == "command_only":
                        prompt = (
                            "You are a shell assistant. Output ONLY the raw executable shell command. Do not use markdown formatting. Do not provide explanations or be verbose.\n\nUser request: "
                            + transcript
                        )
                    elif mode == "speak_only":
                        prompt = (
                            "You are a shell assistant. Provide a highly concise conversational reply. Do not be verbose. Do not output executable commands.\n\nUser request: "
                            + transcript
                        )
                    else:
                        prompt = (
                            "You are a shell assistant. Provide a highly concise conversational reply, and enclose the executable shell command inside a single ```bash code block. Do not be verbose.\n\nUser request: "
                            + transcript
                        )

                    raw_response = self.thinker.ask(prompt)

                    speech_text = ""
                    command_text = ""

                    if mode == "command_only":
                        command_text = raw_response.strip()
                    elif mode == "speak_only":
                        speech_text = raw_response.strip()
                    else:
                        # Parse markdown blocks
                        import re

                        blocks = re.findall(r"```(?:bash|sh)?\n?(.*?)```", raw_response, re.DOTALL)
                        if blocks:
                            command_text = "\n".join(b.strip() for b in blocks)
                            speech_text = re.sub(r"```(?:bash|sh)?\n?.*?```", "", raw_response, flags=re.DOTALL).strip()
                        else:
                            # Fallback if the LLM forgot the block
                            speech_text = raw_response.strip()

                    self._set_cursor_state("typing")
                    if command_text:
                        self._inject_command(command_text)

                    # Queue TTS response if enabled
                    if self.tts_provider and speech_text:
                        self._set_cursor_state("speaking")
                        self.tts_queue.put(speech_text)
                    else:
                        self._set_cursor_state("idle")
                except Exception as e:
                    sys.stderr.write(f"\r\n[vsh] Thinker Error: {e}\r\n")
                    self._set_cursor_state("idle")
            else:
                # Direct injection
                self._inject_command(transcript)
                self._set_cursor_state("idle")

            self.stt_queue.task_done()

    def _tts_worker(self):
        """Background worker to handle TTS synthesis and playback without blocking the shell."""
        while True:
            text = self.tts_queue.get()
            if text is None:
                break
            try:
                wav = self.tts_provider.synthesize(text)
                # Convert to int16 bytes
                data = (wav * 32767 * 0.9).astype("int16").tobytes()
                from vsh.core.audio import AudioSignal

                # Use the configured device index for TTS if available
                dev = self.config.tts.device_index if hasattr(self.config.tts, "device_index") else None
                AudioSignal(data, 44100).play(device_index=dev)
            except Exception as e:
                logger.error(f"TTS Error: {e}")
            finally:
                self._set_cursor_state("idle")
                self.tts_queue.task_done()

    def _setup_terminal(self):
        """Save terminal state and enter raw mode."""
        self.old_tty_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def _restore_terminal(self):
        """Restore original terminal state and cursor."""
        if self.old_tty_attrs:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, self.old_tty_attrs)
            sys.stdout.buffer.write(CURSOR_DEFAULT)
            sys.stdout.buffer.flush()

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
            # Set it on the PTY
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
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
        """Update terminal cursor color based on processing state to avoid terminal text pollution."""
        if text is not None:
            self._current_transcript = text
            self._transcript_time = time.time()

        if state in ("transcribing", "thinking", "typing", "speaking"):
            self._pipeline_state = state
            if hasattr(self, "voice_thread"):
                self.voice_thread.is_processing = True
        elif state == "idle":
            self._pipeline_state = None
            if hasattr(self, "voice_thread"):
                self.voice_thread.is_processing = False

        self._current_cursor_state = state
        if state == "listening_active":
            sys.stdout.buffer.write(b"\033]12;#ff00ff\a\033[1 q")  # Bright Pink blink
        elif state == "listening_idle":
            sys.stdout.buffer.write(b"\033]12;#880000\a\033[1 q")  # Dark Red blink
        elif state == "transcribing":
            sys.stdout.buffer.write(b"\033]12;#00ffff\a\033[1 q")  # Cyan
        elif state == "thinking":
            sys.stdout.buffer.write(b"\033]12;#ffa500\a\033[3 q")  # Orange underline
        elif state in ("typing", "speaking"):
            sys.stdout.buffer.write(b"\033]12;#00ff00\a\033[4 q")  # Green underline
        else:
            sys.stdout.buffer.write(CURSOR_DEFAULT)

        sys.stdout.buffer.flush()
        self._render_ui()

    def _notify(self, msg: str, color="36"):
        """Legacy text notify for verbose mode only."""
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
        # We don't append newline! The user types Enter themselves.
        try:
            os.write(self.master_fd, cmd.encode())
        except OSError as e:
            logger.error(f"Failed to write to PTY: {e}")

    def run(self):
        """Main entry point: fork PTY, start threads, and multiplex I/O."""
        # Set recursion guard so inner shell doesn't spawn vsh again
        os.environ["VSH_ACTIVE"] = "1"

        # Fork the PTY
        pid, self.master_fd = pty.fork()

        if pid == 0:
            # Child process: execute the shell
            try:
                os.execv(self.inner_shell, [self.inner_shell])
            except Exception as e:
                sys.stderr.write(f"[vsh] Failed to start shell: {self.inner_shell}: {e}\n")
                os._exit(1)
        else:
            # Parent process: act as proxy

            # Setup signal handlers
            signal.signal(signal.SIGINT, self._handle_sigint)
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
            self._handle_sigwinch(None, None)

            # Start background STT thread
            self.voice_thread.start()

            # The pipeline worker handles LLM/Thinking
            self.pipeline_thread = threading.Thread(target=self._pipeline_worker, daemon=True)
            self.pipeline_thread.start()

            if self.tts_provider:
                self.tts_worker = threading.Thread(target=self._tts_worker, daemon=True)
                self.tts_worker.start()

            # Setup raw mode
            self._setup_terminal()

            # Defer UI updates slightly so the inner shell doesn't overwrite them
            def show_startup_ui():
                time.sleep(0.5)
                if self.verbose:
                    self._notify(f"VSH active. Press {self.config.keybinds.toggle_listen} or Ctrl+G to toggle.")
                if self.config.shell.voice_on_start:
                    # We inject a simulated toggle keypress into our own loop so it runs safely
                    os.write(self.master_fd, b"\x00")  # A dummy byte to wake up select
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
                if self.tts_provider:
                    self.tts_queue.put(None)
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

    def _render_ui(self):
        """Render the 3-character state indicator and text in the top-right corner."""

        braille_spin = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
        idle_pulse = [" • ", "(•)", "◖•◗", "(•)"]

        state = getattr(self, "_current_cursor_state", "idle")

        # If the software toggle is entirely OFF, vsh goes to sleep visually.
        # Hide the UI completely so it behaves exactly like a normal shell.
        if not getattr(self, "is_listening", False):
            if self.cols > 16:
                last_pos = getattr(self, "_last_ui_pos", self.cols - 15)
                sys.stdout.buffer.write(f"\033[s\033[{last_pos}G\033[K\033[u".encode())
                sys.stdout.buffer.flush()
            return

        indicator = "   "
        text = ""

        # Select indicator and text
        braille_spin = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idle_pulse = [" • ", "(•)", "◖•◗", "(•)"]

        if state == "idle":
            indicator = idle_pulse[(self._anim_frame // 4) % len(idle_pulse)]
            text = "Idle"
        elif state == "listening_idle":
            if getattr(self, "_last_energy", 0) == 0:
                indicator = " ─ "
                text = "Mute"
            else:
                indicator = idle_pulse[(self._anim_frame // 4) % len(idle_pulse)]
                text = "Idle"
        elif state == "listening_active":
            thr = getattr(self, "_last_threshold", getattr(self.config.stt, "vad_threshold", 1000))
            f = self._anim_frame % 4
            if getattr(self, "_last_energy", 0) > thr * 5:
                indicator = ["⠿⠶⠤", "⠶⠿⠶", "⠤⠶⠿", "⠶⠿⠶"][f]
            elif getattr(self, "_last_energy", 0) > thr * 2.5:
                indicator = ["⠶⠤⠤", "⠤⠶⠤", "⠤⠤⠶", "⠤⠶⠤"][f]
            else:
                indicator = ["⠒⠤⠤", "⠤⠒⠤", "⠤⠤⠒", "⠤⠒⠤"][f]
            text = "Listening"
        elif state in ("transcribing", "thinking"):
            indicator = f" {braille_spin[self._anim_frame % len(braille_spin)]} "
            text = "Processing"
        elif state in ("typing", "speaking"):
            indicator = "⠶⠿⠶" if self._anim_frame % 2 == 0 else "⠤⠶⠤"
            text = "Speaking"

        self._anim_frame += 1

        show_text = getattr(self.config.shell, "show_state_text", True)

        if self.cols > 16:
            if show_text:
                # Format: indicator + space + text (padded to 10 chars)
                display_text = f"{indicator} {text:<10}"
            else:
                display_text = f"{indicator}"

            transcript_display = ""
            t = ""

            # Linger transcript for 3 seconds ONLY if idle (useful for Direct Injection to see what was injected)
            linger_time = 3.0
            time_since_transcript = time.time() - getattr(self, "_transcript_time", 0)
            show_transcript_condition = state in ("transcribing", "thinking") or (
                state == "idle" and time_since_transcript < linger_time
            )

            if not show_transcript_condition:
                self._current_transcript = ""

            if (
                show_transcript_condition
                and getattr(self.config.shell, "show_transcript", True)
                and getattr(self, "_current_transcript", "")
                and self.cols >= 40
            ):
                t = self._current_transcript.replace("\n", " ").replace("\r", "")
                max_len = self.cols - len(display_text) - 6
                if len(t) > max_len:
                    t = t[: max_len - 3] + "..."
                transcript_display = f"\033[90m{t}\033[0m "

            # \033[s saves cursor. \033[{col}G moves cursor to far right of current row. \033[K clears to end of line. \033[u restores cursor.
            color = "\033[36m" if state == "idle" else "\033[1;35m"  # Cyan for idle, Magenta for active

            # Position = cols - length of display_text - length of the raw transcript text (no ansi codes)
            pos = max(1, self.cols - len(display_text) - 1 - (len(t) + 1 if transcript_display else 0))

            last_pos = getattr(self, "_last_ui_pos", pos)
            clear_str = ""
            if pos > last_pos:
                # The widget shrank (e.g. transcript disappeared). Clear the leftover text from its previous far-left position.
                clear_str = f"\033[{last_pos}G\033[K"
            self._last_ui_pos = pos

            ui_str = f"\033[s{clear_str}\033[{pos}G{transcript_display}{color}{display_text}\033[0m\033[K\033[u"
            sys.stdout.buffer.write(ui_str.encode())
            sys.stdout.buffer.flush()

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

            self._render_ui()

            if self._interrupted:
                logger.info("Interrupted by signal")
                break

            if sys.stdin.fileno() in ready_r:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                if not data:
                    logger.info("EOF on stdin (Ctrl+D)")
                    # Forward EOT to child so the shell can process it gracefully
                    os.write(self.master_fd, b"\x04")
                    break

                with open("/tmp/vsh_keys.log", "a") as f:
                    f.write(repr(data) + "\n")

                if any(t in data for t in self.triggers):
                    for t in self.triggers:
                        data = data.replace(t, b"")
                    self._toggle_listening()

                # Forward everything else to PTY
                if data:
                    os.write(self.master_fd, data)

            # 2. Process PTY output
            if self.master_fd in ready_r:
                try:
                    data = os.read(self.master_fd, 10240)
                except OSError:
                    break  # Child exited

                if not data:
                    break

                sys.stdout.buffer.write(data)
                # ponytail: force cursor state after PTY redraws (e.g. prompt resets)
                state = getattr(self, "_current_cursor_state", "idle")
                if state != "idle":
                    self._set_cursor_state(state)
                sys.stdout.buffer.flush()
