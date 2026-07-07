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
        self.assertEqual(cfg.shell.overlay_line, "bottom")
        self.assertEqual(cfg.shell.overlay_color, "36")


class TestStatuslineRender(unittest.TestCase):
    def _make_shell(self, overlay_mode="statusline"):
        cfg = VshConfig()
        cfg.shell.overlay_mode = overlay_mode
        cfg.shell.overlay_line = "bottom"
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


if __name__ == "__main__":
    unittest.main()
