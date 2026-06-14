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
CURSOR_DEFAULT = b"\033]112\033\\\033[2 q\033]2;vsh\033\\"
CURSOR_RED_BLINK = b"\033]12;rgb:ff/00/ff\033\\\033[1 q\033]2;vsh [LISTENING]\033\\"

class PtyShell:
    def __init__(self, config: VshConfig, thinker: Thinker = None):
        self.config = config
        self.thinker = thinker
        
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

    def _notify(self, msg: str):
        """Print a notification on a new line, then restore the cursor."""
        # \r to go to start, \n for new line, print msg, \n to move down
        # Actually in raw mode we need explicit \r\n
        sys.stderr.buffer.write(f"\r\n\033[36m[vsh]\033[0m {msg}\r\n".encode())
        sys.stderr.buffer.flush()

    def _toggle_listening(self):
        self.is_listening = self.voice_thread.toggle_listening()
        if self.is_listening:
            self._notify("Listening...")
        else:
            self._notify("Stopped")
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
            self.voice_thread = VoiceInputThread(self.stt_queue)
            self.voice_thread.start()
            
            # Setup raw mode
            self._setup_terminal()
            self._notify("VSH active. Press Ctrl+\\ to toggle voice.")
            
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
                    if self.thinker:
                        # Route through LLM
                        response = self.thinker.ask(transcript)
                        if isinstance(response, str):
                            self._inject_command(response)
                        else:
                            if response.command:
                                self._inject_command(response.command)
                            if response.speech:
                                # Play TTS in background or block?
                                # For now we notify and let the TTS provider handle it
                                self._notify(f"VSH: {response.speech}")
                                # TODO: wire up actual TTS here when we add TTS to the shell
                    else:
                        # Direct injection
                        self._inject_command(transcript)
            
            # 2. Process keyboard input
            if sys.stdin.fileno() in ready_r:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                    
                if not data:
                    break # EOF
                
                # Check for Ctrl+\ (0x1c)
                if b'\x1c' in data: data = data.replace(b'\x1c', b''); self._toggle_listening()
                
                # Forward everything else to PTY
                if data:
                    os.write(self.master_fd, data)
            
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
