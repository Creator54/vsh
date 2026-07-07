import struct
import termios
import unittest
from unittest.mock import MagicMock, patch

from vsh.core.config import VshConfig
from vsh.core.pty_shell import PtyShell


class _FakeVoiceThread:
    """Minimal stand-in for VoiceInputThread so PtyShell can be built without audio."""

    is_listening = False

    def toggle_listening(self):
        self.is_listening = not self.is_listening
        return self.is_listening


class TestOverlayConfig(unittest.TestCase):
    def test_default_overlay_mode_is_cursor(self):
        cfg = VshConfig()
        self.assertEqual(cfg.shell.overlay_mode, "cursor")
        self.assertEqual(cfg.shell.overlay_color, "36")


class TestStatuslineRender(unittest.TestCase):
    def _make_shell(self, overlay_mode="statusline"):
        cfg = VshConfig()
        cfg.shell.overlay_mode = overlay_mode
        with patch("vsh.core.pty_shell.VoiceInputThread", return_value=_FakeVoiceThread()):
            shell = PtyShell(cfg, thinker=None, verbose=False, tts_provider=None)
        shell.master_fd = 7  # dummy fd; not used by render
        shell.rows, shell.cols = 24, 80
        shell._current_cursor_state = "idle"
        shell._current_transcript = ""
        return shell

    def test_statusline_writes_reserved_bottom_row(self):
        shell = self._make_shell()
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            shell._render_statusline()
        written = buf.write.call_args[0][0].decode()
        # Should target the bottom row (24), clear it, and not use save/restore into
        # the user's scrollback in a way that pollutes it.
        self.assertIn("[24;1H", written)
        self.assertIn("[K", written)
        self.assertIn("vsh", written)

    def test_statusline_hidden_in_cursor_mode(self):
        shell = self._make_shell(overlay_mode="cursor")
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            shell._render_statusline()
        buf.write.assert_not_called()

    def test_statusline_shows_transcript_while_thinking(self):
        shell = self._make_shell()
        shell._current_cursor_state = "thinking"
        shell._current_transcript = "list files in home"
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            shell._render_statusline()
        written = buf.write.call_args[0][0].decode()
        self.assertIn("list files in home", written)


class TestHeadlessImport(unittest.TestCase):
    def test_main_imports_without_inquirerpy(self):
        # importing vsh.core.config must not require InquirerPy at module load
        import importlib

        import vsh.core.config as cfg_mod

        importlib.reload(cfg_mod)
        self.assertTrue(hasattr(cfg_mod, "VshConfig"))


class TestCursorPollution(unittest.TestCase):
    """Transparent overlay modes must NOT recolor the user's terminal cursor."""

    def _make_shell(self, overlay_mode):
        cfg = VshConfig()
        cfg.shell.overlay_mode = overlay_mode
        with patch("vsh.core.pty_shell.VoiceInputThread", return_value=_FakeVoiceThread()):
            shell = PtyShell(cfg, thinker=None, verbose=False, tts_provider=None)
        shell.master_fd = 7
        shell.rows, shell.cols = 24, 80
        return shell

    def test_statusline_does_not_recolor_cursor(self):
        shell = self._make_shell("statusline")
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            # Every state transition should leave the cursor untouched in transparent mode
            for state in ("listening_active", "transcribing", "thinking", "speaking", "idle"):
                shell._set_cursor_state(state)
        # OSC 12 (cursor color) sequence must never be emitted
        for call in buf.write.call_args_list:
            self.assertNotIn(b"\x1b]12;", call[0][0])

    def test_none_mode_does_not_recolor_cursor(self):
        shell = self._make_shell("none")
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            shell._set_cursor_state("listening_active")
        for call in buf.write.call_args_list:
            self.assertNotIn(b"\x1b]12;", call[0][0])

    def test_cursor_mode_still_recolors(self):
        shell = self._make_shell("cursor")
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            shell._set_cursor_state("listening_active")
        self.assertTrue(any(b"\x1b]12;" in call[0][0] for call in buf.write.call_args_list))


class TestCursorTypingCollision(unittest.TestCase):
    """The legacy 'cursor' overlay must NOT paint its HUD on the user's typing row.

    Regression guard for: HUD overlapping typed input until the next line wraps.
    Fixed by reserving a bottom row (PTY shrunk by 1) and drawing on it absolutely.
    """

    def _make_shell(self):
        cfg = VshConfig()
        cfg.shell.overlay_mode = "cursor"
        with patch("vsh.core.pty_shell.VoiceInputThread", return_value=_FakeVoiceThread()):
            shell = PtyShell(cfg, thinker=None, verbose=False, tts_provider=None)
        shell.master_fd = 7
        shell.rows, shell.cols = 24, 80
        shell._current_cursor_state = "listening_idle"
        shell.is_listening = True
        return shell

    def test_sigwinch_shrinks_pty_by_one_row(self):
        # Patch ioctl to capture the size handed to the PTY
        captured = {}

        def fake_ioctl(fd, request, arg):
            if request == termios.TIOCSWINSZ:
                r, c, *_ = struct.unpack("HHHH", arg)
                captured["rows"] = r
                captured["cols"] = c
            return b""

        shell = self._make_shell()
        with patch("fcntl.ioctl", side_effect=fake_ioctl), patch("termios.TIOCGWINSZ", termios.TIOCGWINSZ, create=True):
            # _handle_sigwinch reads size from stdout; give it a fake size
            with patch(
                "fcntl.ioctl",
                side_effect=lambda fd, req, arg: (
                    struct.pack("hh", 24, 80) if req == termios.TIOCGWINSZ else fake_ioctl(fd, req, arg)
                ),
            ):
                shell._handle_sigwinch(None, None)
        self.assertEqual(captured["rows"], 23)  # 24 - 1 reserved
        self.assertEqual(captured["cols"], 80)

    def test_hud_renders_on_reserved_bottom_row_not_typing_row(self):
        shell = self._make_shell()
        buf = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            shell._render_ui()
        written = buf.write.call_args[0][0].decode()
        # HUD must target the bottom row (24) absolutely...
        self.assertIn("[24;", written)
        # ...and must never jump to a column on the current (unspecified) row via
        # bare "\033[<col>G" (that's what caused the typing collision).
        self.assertNotIn("G\033[K", written)
        # HUD label is present on its own reserved row (idle w/ zero energy -> "Mute")
        self.assertIn("Mute", written)


if __name__ == "__main__":
    unittest.main()
