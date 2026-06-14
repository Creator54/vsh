import os
import pty
import select
import sys
import termios
import tty
import signal
import fcntl
import struct
import queue
import time
from loguru import logger
from vsh.core.config import VshConfig
from vsh.core.voice_input import VoiceInputThread
from vsh.core.provider import Thinker, ThinkerResponse

# ANSI escape sequences for cursor control and terminal title
CURSOR_DEFAULT = b"\033]112\a\033[0 q\033]0;vsh\a"
CURSOR_RED_BLINK = b"\033]12;#ff00ff\a\033[1 q\033]0;vsh [LISTENING]\a"

class PtyShell:
    def __init__(self, config: VshConfig, thinker: Thinker = None, verbose: bool = False, tts_provider=None):
        self.config = config
        self.thinker = thinker
        self.verbose = verbose
        self.tts_provider = tts_provider
        
        # Determine which shell to run
        self.inner_shell = config.shell.inner_shell or os.environ.get("SHELL", "/bin/bash")
        
        self.master_fd = None
        self.slave_fd = None
        self.stt_queue = queue.Queue()
        self.voice_thread = None
        
        # State
        self.is_listening = False
        self.old_tty_attrs = None

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

    def _handle_sigwinch(self, signum, frame):
        """Propagate window resize to the PTY."""
        if self.master_fd is None:
            return
        
        # Get terminal size from stdout
        try:
            buf = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'0000')
            rows, cols = struct.unpack('hh', buf)
            # Set it on the PTY
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        except Exception:
            pass

    def _update_cursor(self):
        if self.is_listening:
            sys.stdout.buffer.write(CURSOR_RED_BLINK)
        else:
            sys.stdout.buffer.write(CURSOR_DEFAULT)
        sys.stdout.buffer.flush()

    def _notify(self, msg: str, color="36"):
        """Print a notification on a new line, then restore the cursor."""
        # \r to go to start, \n for new line, print msg, \n to move down
        # Actually in raw mode we need explicit \r\n
        sys.stderr.buffer.write(f"\r\n\033[{color}m[vsh]\033[0m {msg}\r\n".encode())
        sys.stderr.buffer.flush()

    def _toggle_listening(self):
        self.is_listening = self.voice_thread.toggle_listening()
        m = "LISTENING" if self.is_listening else "STOPPED"
        c = "1;35" if self.is_listening else "36"
        self._notify(m, color=c)
        if self.is_listening: sys.stdout.buffer.write(b"\a")
        self._update_cursor()

    def _inject_command(self, cmd: str):
        """Inject text directly into the PTY."""
        if not cmd:
            return
        # We don't append newline! The user types Enter themselves.
        os.write(self.master_fd, cmd.encode())

    def run(self):
        """Main entry point: fork PTY, start threads, and multiplex I/O."""
        # Set recursion guard so inner shell doesn't spawn vsh again
        os.environ["VSH_ACTIVE"] = "1"
        
        # Fork the PTY
        pid, self.master_fd = pty.fork()
        
        if pid == 0:
            # Child process: execute the shell
            os.execv(self.inner_shell, [self.inner_shell])
        else:
            # Parent process: act as proxy
            
            # Setup signal handler for resize
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
            self._handle_sigwinch(None, None)
            
            # Start background STT thread
            self.voice_thread = VoiceInputThread(
                self.stt_queue, 
                provider_name=self.config.stt.provider,
                device_index=self.config.stt.device_index,
                verbose=self.verbose,
                vad_threshold=self.config.stt.vad_threshold,
                vad_silence_limit=self.config.stt.vad_silence_limit
            )
            self.voice_thread.start()
            
            # Setup raw mode
            self._setup_terminal()
            self._notify("VSH active (LOCAL VERSION). Press Ctrl+\\ to toggle.")
            
            try:
                self._io_loop()
            finally:
                # Cleanup
                self._restore_terminal()
                self.voice_thread.stop()
                self.voice_thread.join(timeout=2.0)
                os.waitpid(pid, 0)

    def _io_loop(self):
        """The main select() loop that multiplexes stdin, PTY, and STT results."""
        while True:
            # Read inputs: stdin and the PTY master
            r_fds = [sys.stdin.fileno(), self.master_fd]
            
            try:
                # Wait for I/O with a short timeout so we can check the STT queue
                ready_r, _, _ = select.select(r_fds, [], [], 0.1)
            except InterruptedError:
                continue
            
            # 1. Process STT queue
            while not self.stt_queue.empty():
                transcript = self.stt_queue.get()
                if transcript:
                    if self.verbose: logger.info(f"STT Transcript: {transcript}")
                    if self.thinker:
                        # Route through LLM
                        response = self.thinker.ask(transcript)
                        if isinstance(response, str):
                            self._inject_command(response)
                        else:
                            if response.command:
                                self._inject_command(response.command)
                            if response.speech:
                                # Play TTS
                                self._notify(f"VSH: {response.speech}")
                                if self.tts_provider:
                                    try:
                                        wav = self.tts_provider.synthesize(response.speech)
                                        # Convert to int16 bytes
                                        data = (wav * 32767 * 0.9).astype("int16").tobytes()
                                        from vsh.core.audio import AudioSignal
                                        AudioSignal(data, 44100).play()
                                    except Exception as e:
                                        logger.error(f"TTS Error: {e}")
                    else:
                        # Direct injection
                        self._inject_command(transcript)
            
            # 2. Process keyboard input
            if sys.stdin.fileno() in ready_r:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError: break
                if not data: break
                
                if self.verbose:
                    self._notify(f"RAW INPUT: {data.hex(' ')}", color="33")

                # Triggers: standard bytes or Kitty Protocol sequences
                # 1c=Ctrl+\, 07=Ctrl+G, CSI 92;5u=Kitty Ctrl+\, CSI 103;5u=Kitty Ctrl+G
                triggers = [b'\x1c', b'\x07', b'\x1b[92;5u', b'\x1b[103;5u']
                if any(t in data for t in triggers):
                    for t in triggers: data = data.replace(t, b'')
                    self._toggle_listening()
                
                # Forward everything else to PTY
                if data: os.write(self.master_fd, data)
            
            # 3. Process PTY output
            if self.master_fd in ready_r:
                try:
                    data = os.read(self.master_fd, 10240)
                except OSError:
                    break # Child exited
                
                if not data:
                    break
                
                sys.stdout.buffer.write(data)
                # ponytail: force cursor state after PTY redraws (e.g. prompt resets)
                if self.is_listening:
                    sys.stdout.buffer.write(CURSOR_RED_BLINK)
                sys.stdout.buffer.flush()
