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
from vsh.core.provider import Thinker
from vsh.core.voice_input import VoiceInputThread

# ANSI escape sequences for cursor control and terminal title
CURSOR_DEFAULT = b"\033]112\a\033[0 q\033]0;vsh\a"
CURSOR_RED_BLINK = b"\033]12;#ff00ff\a\033[1 q\033]0;vsh [LISTENING]\a"


class PtyShell:
    def __init__(self, config: VshConfig, thinker: Thinker = None, verbose: bool = False, tts_provider=None):
        self.config = config
        self.thinker = thinker
        self.verbose = verbose
        self.tts_provider = tts_provider

        # Precompute keybind triggers to avoid loop overhead
        k = self.config.keybinds.toggle_listen.lower()
        self.triggers = []
        if k in ("ctrl+\\", "ctrl+\\\\"):
            self.triggers = [b"\x1c", b"\x1b[92;5u", b"\x1b[92;133u", b"\x1b[28;5u", b"\x1b[28;133u"]
        elif k == "ctrl+g":
            self.triggers = [b"\x07", b"\x1b[103;5u", b"\x1b[103;133u"]
        else:
            self.triggers = [b"\x1c", b"\x1b[92;5u", b"\x1b[92;133u", b"\x1b[28;5u", b"\x1b[28;133u"]  # default

        # Always provide Ctrl+G as a fallback (5u = Ctrl, 133u = Ctrl+NumLock)
        self.triggers.extend([b"\x07", b"\x1b[103;5u", b"\x1b[103;133u"])

        # Determine which shell to run
        self.inner_shell = config.shell.inner_shell or os.environ.get("SHELL", "/bin/bash")

        self.master_fd = None
        self.slave_fd = None
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
        )
        self.pipeline_thread = None
        self.tts_worker = None
        self.is_listening = False
        self.old_tty_attrs = None
        self._interrupted = False

    def _pipeline_worker(self):
        """Background worker to handle thinking and coordination without blocking the shell."""
        while True:
            transcript = self.stt_queue.get()
            if transcript is None:
                break

            if self.verbose:
                logger.info(f"Processing: {transcript}")
            else:
                self._set_cursor_state("transcribing")

            if self.thinker:
                if not self.verbose:
                    self._set_cursor_state("thinking")
                try:
                    response = self.thinker.ask(transcript)
                    if not self.verbose:
                        self._set_cursor_state("typing")
                    self._inject_command(response)
                except Exception as e:
                    logger.error(f"Thinker Error: {e}")
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
            # Set it on the PTY
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _volume_callback(self, energy: int, threshold: int):
        """Update cursor color based on voice activity."""
        if not getattr(self, "is_listening", False):
            return

        pipeline = getattr(self, "_pipeline_state", None)

        if energy > threshold:
            state = "listening_active"
        else:
            # If we are processing something, don't fall back to listening_idle,
            # stay in the processing state so the user knows it's thinking!
            state = pipeline if pipeline else "listening_idle"

        current = getattr(self, "_current_cursor_state", "idle")
        # Only issue escape sequence if state actually changed to avoid PTY flicker
        if current != state:
            self._set_cursor_state(state)

    def _set_cursor_state(self, state: str):
        """Update terminal cursor color based on processing state to avoid terminal text pollution."""
        if state in ("transcribing", "thinking", "typing"):
            self._pipeline_state = state
        elif state == "idle":
            self._pipeline_state = None

        self._current_cursor_state = state
        if state == "listening_active":
            sys.stdout.buffer.write(b"\033]12;#ff00ff\a\033[1 q")  # Bright Pink blink
        elif state == "listening_idle":
            sys.stdout.buffer.write(b"\033]12;#880000\a\033[1 q")  # Dark Red blink
        elif state == "transcribing":
            sys.stdout.buffer.write(b"\033]12;#00ffff\a\033[1 q")  # Cyan
        elif state == "thinking":
            sys.stdout.buffer.write(b"\033]12;#ffff00\a\033[1 q")  # Yellow
        elif state == "typing":
            sys.stdout.buffer.write(b"\033]12;#00ff00\a\033[1 q")  # Green
        else:
            sys.stdout.buffer.write(CURSOR_DEFAULT)
        sys.stdout.buffer.flush()

    def _notify(self, msg: str, color="36"):
        """Legacy text notify for verbose mode only."""
        if self.verbose:
            sys.stdout.buffer.write(f"\r\n\033[{color}m[vsh]\033[0m {msg}\r\n".encode())
            sys.stdout.buffer.flush()

    def _clear_notify(self):
        pass

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
                ready_r, _, _ = select.select(r_fds, [], [], 0.2)
            except InterruptedError:
                continue

            if self._interrupted:
                logger.info("Interrupted by signal")
                break

            # 1. Process keyboard input
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
