import os
import signal
import stat
import struct
import tempfile
import termios
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import vsh.core.pty_shell as pty_module
from vsh.core.config import VshConfig, load_config
from vsh.core.pty_shell import CURSOR_RESET, PtyShell
from vsh.core.voice_input import VoiceState


class _FakeVoiceThread:
    def __init__(self, state_callback=None):
        self.is_listening = False
        self.is_processing = False
        self.system_mic_muted = None
        self.state_callback = state_callback

    def toggle_listening(self):
        self.is_listening = not self.is_listening
        if self.state_callback:
            self.state_callback(VoiceState.IDLE if self.is_listening else None)
        return self.is_listening

    def set_system_mic_muted(self, muted):
        self.system_mic_muted = muted

    def set_processing(self, processing):
        self.is_processing = processing

    def suppress_input(self, duration=0.6):
        pass


class OverlayTests(unittest.TestCase):
    def _make_shell(self, mode="auto"):
        cfg = VshConfig()
        cfg.shell.overlay_mode = mode
        with patch(
            "vsh.core.pty_shell.VoiceInputThread",
            side_effect=lambda *_args, **kwargs: _FakeVoiceThread(kwargs.get("state_callback")),
        ):
            shell = PtyShell(cfg, thinker=None, verbose=False, tts_provider=None)
        shell.master_fd = 7
        shell.rows, shell.cols = 24, 80
        shell._current_cursor_state = VoiceState.IDLE
        shell.voice_thread.is_listening = True
        return shell

    def test_default_overlay_mode_is_auto(self):
        self.assertEqual(VshConfig().shell.overlay_mode, "auto")

    def test_ctrl_close_bracket_is_a_listening_trigger(self):
        shell = self._make_shell("auto")
        self.assertIn(b"\x1d", shell.triggers)

    def test_invalid_mode_falls_back_to_auto(self):
        shell = self._make_shell("statusline")
        shell.old_tty_attrs = object()
        with patch.object(shell, "_probe_graphics_support", return_value=False):
            shell._select_visual_mode()
        self.assertEqual(shell._visual_mode, "cursor")

    def test_probe_accepts_graphics_response_before_device_attributes(self):
        shell = self._make_shell("auto")
        shell.old_tty_attrs = object()
        response = f"\x1b_Gi={shell._image_id};OK\x1b\\\x1b[?1;2cuser".encode()
        with (
            patch("sys.stdout") as stdout,
            patch("sys.stdin.fileno", return_value=0),
            patch("select.select", return_value=([0], [], [])),
            patch("os.read", return_value=response),
        ):
            stdout.buffer = MagicMock()
            self.assertTrue(shell._probe_graphics_support(timeout=0.25))
        self.assertEqual(shell._pending_input, b"user")

    def test_probe_rejects_device_attributes_without_graphics(self):
        shell = self._make_shell("auto")
        shell.old_tty_attrs = object()
        with (
            patch("sys.stdout") as stdout,
            patch("sys.stdin.fileno", return_value=0),
            patch("select.select", return_value=([0], [], [])),
            patch("os.read", return_value=b"\x1b[?1;2c"),
        ):
            stdout.buffer = MagicMock()
            self.assertFalse(shell._probe_graphics_support(timeout=0.25))

    def test_graphics_mode_keeps_full_terminal_height(self):
        captured = {}

        def fake_ioctl(fd, request, arg):
            if request == termios.TIOCSWINSZ:
                captured["rows"], captured["cols"], *_ = struct.unpack("HHHH", arg)
            return struct.pack("hh", 24, 80) if request == termios.TIOCGWINSZ else b""

        shell = self._make_shell("kitty")
        with patch("fcntl.ioctl", side_effect=fake_ioctl):
            shell._handle_sigwinch(None, None)
        self.assertEqual(captured, {"rows": 24, "cols": 80})

    def test_graphics_badge_is_compact_and_text_safe(self):
        shell = self._make_shell("kitty")
        shell._visual_mode = "graphics"
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell._render_graphics_badge()
        written = buf.write.call_args[0][0]
        self.assertIn(b"a=t", written)
        self.assertIn(b"a=p", written)
        self.assertLess(written.index(b"a=t"), written.index(b"a=p"))
        self.assertIn(b"c=2,r=1,C=1,z=1", written)
        self.assertNotIn(b"\033[K", written)
        self.assertNotIn(b"Listening", written)
        self.assertNotIn(b"\033[1;35m", written)
        self.assertIn(b"\033[?25l", written)
        self.assertIn(b"\033[1;79H", written)

    def test_inactive_vsh_removes_the_graphics_badge(self):
        shell = self._make_shell("kitty")
        shell._visual_mode = "graphics"
        shell._graphics_visible = True
        buf = MagicMock()

        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell.voice_thread.is_listening = False
            shell._current_cursor_state = None
            shell._render_graphics_badge()

        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"a=d,d=I", written)
        self.assertNotIn(b"a=t", written)
        self.assertNotIn(b"a=p", written)
        self.assertIn(b"\033[?25h", written)
        self.assertFalse(shell._graphics_visible)

    def test_graphics_redraw_deletes_the_previous_frame_first(self):
        shell = self._make_shell("kitty")
        shell._visual_mode = "graphics"
        buf = MagicMock()

        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell._set_system_mic_muted(True)
            shell._render_graphics_badge()
            buf.reset_mock()
            shell._set_system_mic_muted(False)
            shell._render_graphics_badge()

        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"a=d,d=I", written)
        self.assertLess(written.index(b"a=d,d=I"), written.index(b"a=t"))

    def test_typing_restores_native_cursor_and_delays_image(self):
        shell = self._make_shell("kitty")
        shell._visual_mode = "graphics"
        shell._graphics_visible = True
        buf = MagicMock()
        with patch("sys.stdout") as stdout, patch("time.monotonic", return_value=10.0):
            stdout.buffer = buf
            shell._user_started_typing()
        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"a=d,d=I", written)
        self.assertIn(b"\033[?25h", written)
        self.assertGreater(shell._typing_until, 10.0)
        self.assertEqual(shell._current_cursor_state, VoiceState.IDLE)

    def test_typing_temporarily_suppresses_microphone_input(self):
        shell = self._make_shell("none")
        shell.voice_thread.suppress_input = MagicMock()

        with patch("time.monotonic", return_value=10.0):
            shell._user_started_typing()

        shell.voice_thread.suppress_input.assert_called_once_with(0.6)

    def test_graphics_cursor_suppressed_in_alternate_screen(self):
        shell = self._make_shell("kitty")
        shell._visual_mode = "graphics"
        shell._alternate_screen = True
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell._render_graphics_badge()
        buf.write.assert_not_called()

    def test_cursor_fallback_writes_only_cursor_controls(self):
        shell = self._make_shell("auto")
        shell._visual_mode = "cursor"
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell._set_cursor_state(VoiceState.LISTENING)
        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"\033]12;", written)
        self.assertNotIn(b"\033[K", written)
        self.assertNotIn(b"vsh:", written)

    def test_raw_volume_updates_intensity_without_changing_state(self):
        shell = self._make_shell("kitty")
        shell._current_cursor_state = VoiceState.IDLE

        shell._volume_callback(9000, 1000)

        self.assertEqual(shell._current_cursor_state, VoiceState.IDLE)
        self.assertEqual(shell._last_energy, 9000)

    def test_system_mic_state_is_independent_from_vsh_activation(self):
        shell = self._make_shell("none")

        shell._set_system_mic_muted(True)
        self.assertTrue(shell.voice_thread.is_listening)
        self.assertEqual(shell._effective_cursor_state(), VoiceState.MUTED)

        shell._set_system_mic_muted(False)
        self.assertTrue(shell.voice_thread.is_listening)
        self.assertEqual(shell._effective_cursor_state(), VoiceState.IDLE)

    def test_vsh_disabled_has_no_visual_even_when_system_mic_is_muted(self):
        shell = self._make_shell("none")
        shell._set_system_mic_muted(True)

        shell._toggle_listening()

        self.assertFalse(shell.voice_thread.is_listening)
        self.assertIsNone(shell._effective_cursor_state())

    def test_system_mute_overrides_processing_visual(self):
        shell = self._make_shell("none")
        shell._set_cursor_state(VoiceState.THINKING)

        shell._set_system_mic_muted(True)

        self.assertEqual(shell._effective_cursor_state(), VoiceState.MUTED)

    def test_voice_status_uses_the_same_effective_state_as_the_hud(self):
        shell = self._make_shell("none")
        shell._set_system_mic_muted(True)

        self.assertEqual(
            shell.voice_status(),
            {
                "enabled": True,
                "mic_muted": True,
                "phase": "idle",
                "visual_state": "muted",
            },
        )

    def test_toggle_uses_idle_and_inactive_states(self):
        shell = self._make_shell("none")
        shell.voice_thread.is_listening = False

        with patch("time.monotonic", side_effect=(10.0, 11.0)):
            shell._toggle_listening()
            self.assertEqual(shell._current_cursor_state, VoiceState.IDLE)
            shell._toggle_listening()
        self.assertIsNone(shell._current_cursor_state)
        self.assertIsNone(shell._effective_cursor_state())

    def test_rapid_toggle_events_are_not_dropped(self):
        shell = self._make_shell("none")
        shell.voice_thread.is_listening = False

        with patch("time.monotonic", side_effect=(10.0, 10.05)):
            shell._toggle_listening()
            shell._toggle_listening()

        self.assertFalse(shell.voice_thread.is_listening)
        self.assertIsNone(shell._current_cursor_state)

    def test_resting_visual_uses_the_microphone_actual_state(self):
        shell = self._make_shell("none")
        shell._set_system_mic_muted(True)

        self.assertEqual(shell._effective_cursor_state(), VoiceState.MUTED)

        shell._set_system_mic_muted(False)

        self.assertEqual(shell._effective_cursor_state(), VoiceState.IDLE)

    def test_processing_returns_to_muted_when_mic_was_toggled_off(self):
        shell = self._make_shell("none")
        shell._set_system_mic_muted(True)
        shell._set_cursor_state(VoiceState.THINKING)

        shell._dispatch_response('{"speech":"","command":null}')

        self.assertEqual(shell._current_cursor_state, VoiceState.IDLE)
        self.assertEqual(shell._effective_cursor_state(), VoiceState.MUTED)

    def test_response_returns_to_inactive_when_voice_was_never_enabled(self):
        shell = self._make_shell("none")
        shell.voice_thread.is_listening = False

        shell._dispatch_response('{"speech":"Done.","command":null}')

        self.assertIsNone(shell._current_cursor_state)

    def test_structured_voice_reply_splits_speech_and_command(self):
        reply = pty_module.parse_voice_reply('{"speech":"Opening Aether.","command":"cd /home/creator54 && Aether"}')

        self.assertEqual(reply.speech, "Opening Aether.")
        self.assertEqual(reply.command, "cd /home/creator54 && Aether")

    def test_malformed_voice_reply_is_speech_only(self):
        reply = pty_module.parse_voice_reply("ordinary unstructured response")

        self.assertEqual(reply.speech, "ordinary unstructured response")
        self.assertEqual(reply.command, "")

    def test_voice_handler_dispatches_response_without_shell_injection(self):
        shell = self._make_shell("none")
        shell.voice_handler = MagicMock()
        shell.voice_handler.ask.return_value = '{"speech":"Ready.","command":"pwd"}'
        shell.stt_queue.put("request with spaces and punctuation; $(safe)")
        shell.stt_queue.put(None)

        with patch.object(shell, "_dispatch_response") as dispatch, patch.object(shell, "_inject_command") as inject:
            shell._pipeline_worker()

        shell.voice_handler.ask.assert_called_once_with("request with spaces and punctuation; $(safe)")
        dispatch.assert_called_once_with('{"speech":"Ready.","command":"pwd"}')
        inject.assert_not_called()

    def test_generic_thinker_receives_the_json_voice_contract(self):
        shell = self._make_shell("none")
        shell.thinker = MagicMock()
        shell.thinker.ask.return_value = '{"speech":"Ready.","command":null}'
        shell.stt_queue.put("tell me the current directory")
        shell.stt_queue.put(None)

        with patch.object(shell, "_dispatch_response"):
            shell._pipeline_worker()

        prompt = shell.thinker.ask.call_args.args[0]
        self.assertIn('"speech":"brief response"', prompt)
        self.assertIn('"command":null', prompt)
        self.assertIn("Fish command string or null", prompt)
        self.assertIn("User request: tell me the current directory", prompt)

    def test_speaker_off_publishes_speech_then_command_in_one_bridge_event(self):
        shell = self._make_shell("none")
        shell.config.shell.auto_submit = True

        with patch.object(shell, "_publish_reply") as publish:
            shell._dispatch_response('{"speech":"Opening it.","command":"cd /tmp"}')

        publish.assert_called_once_with("Opening it.", "cd /tmp")

    def test_speaker_on_plays_speech_before_publishing_command(self):
        events = []
        shell = self._make_shell("none")
        shell.tts_provider = object()
        shell._speak = lambda text: events.append(("speech", text)) or True
        shell._publish_reply = lambda speech, command: events.append(("bridge", speech, command))

        shell._dispatch_response('{"speech":"Opening it.","command":"cd /tmp"}')

        self.assertEqual(
            events,
            [("speech", "Opening it."), ("bridge", "", "cd /tmp")],
        )

    def test_speaker_output_suppresses_its_acoustic_tail(self):
        shell = self._make_shell("none")
        shell.tts_provider = object()
        shell._speak = lambda _text: True
        shell.voice_thread.suppress_input = MagicMock()

        shell._dispatch_response('{"speech":"Opening it.","command":null}')

        shell.voice_thread.suppress_input.assert_called_once_with(0.3)

    def test_spoken_command_uses_speaking_typing_idle_order(self):
        shell = self._make_shell("none")
        shell.tts_provider = object()
        shell._speak = lambda _text: True

        with patch.object(shell, "_set_cursor_state") as set_state, patch.object(shell, "_publish_reply"):
            shell._dispatch_response('{"speech":"Opening it.","command":"cd /tmp"}')

        self.assertEqual(
            [call.args[0] for call in set_state.call_args_list],
            [VoiceState.SPEAKING, VoiceState.TYPING, VoiceState.IDLE],
        )

    def test_failed_speaker_prints_speech_before_command(self):
        shell = self._make_shell("none")
        shell.tts_provider = object()
        shell._speak = lambda _text: False

        with patch.object(shell, "_publish_reply") as publish:
            shell._dispatch_response('{"speech":"Opening it.","command":"cd /tmp"}')

        publish.assert_called_once_with("Opening it.", "cd /tmp")

    def test_fish_response_bridge_writes_ordered_reply_and_signals_shell(self):
        shell = self._make_shell("none")
        shell.config.shell.response_bridge = "fish-signal"
        shell.config.shell.auto_submit = True
        shell.shell_name = "fish"
        shell.shell_pid = 4321

        with (
            tempfile.TemporaryDirectory() as runtime,
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}),
            patch("os.kill") as kill,
        ):
            shell._publish_reply("Voice response", "pwd")
            response = Path(runtime) / "vsh" / "4321.response"
            command = Path(runtime) / "vsh" / "4321.command"
            submit = Path(runtime) / "vsh" / "4321.submit"

            self.assertEqual(response.read_text(), "Voice response\n")
            self.assertEqual(command.read_text(), "pwd")
            self.assertEqual(submit.read_text(), "1\n")
            self.assertEqual(stat.S_IMODE(response.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(command.stat().st_mode), 0o600)
            kill.assert_called_once_with(4321, signal.SIGUSR1)

    def test_fish_response_bridge_leaves_command_editable_when_auto_submit_is_off(self):
        shell = self._make_shell("none")
        shell.config.shell.response_bridge = "fish-signal"
        shell.config.shell.auto_submit = False
        shell.shell_name = "fish"
        shell.shell_pid = 4321

        with (
            tempfile.TemporaryDirectory() as runtime,
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}),
            patch("os.kill"),
        ):
            shell._publish_reply("", "cd /tmp")
            bridge = Path(runtime) / "vsh"

            self.assertEqual((bridge / "4321.command").read_text(), "cd /tmp")
            self.assertFalse((bridge / "4321.submit").exists())

    def test_fish_response_bridge_falls_back_to_the_vsh_home(self):
        shell = self._make_shell("none")
        shell.config.shell.response_bridge = "fish-signal"
        shell.shell_name = "fish"
        shell.shell_pid = 4321

        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}), patch("os.kill"):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            shell._publish_reply("Voice response", "")

            response = Path(home) / ".vsh" / "run" / "4321.response"
            self.assertEqual(response.read_text(), "Voice response\n")

    def test_response_bridge_uses_stdout_for_non_fish_shells(self):
        shell = self._make_shell("none")
        shell.config.shell.response_bridge = "fish-signal"
        shell.shell_name = "bash"
        shell.shell_pid = 4321

        with patch("sys.stdout") as stdout, patch("os.kill") as kill:
            shell._publish_reply("Voice response", "")

        stdout.write.assert_called_once_with("\r\nVoice response\r\n")
        kill.assert_not_called()

    def test_graphics_cleanup_is_targeted(self):
        shell = self._make_shell("kitty")
        shell._graphics_visible = True
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell._delete_graphics_badge()
        written = buf.write.call_args[0][0]
        self.assertIn(b"a=d,d=I", written)
        self.assertNotIn(b"a=d,d=a", written)

    def test_none_mode_is_visual_noop(self):
        shell = self._make_shell("none")
        shell.old_tty_attrs = object()
        shell._select_visual_mode()
        self.assertEqual(shell._visual_mode, "none")
        self.assertEqual(CURSOR_RESET, b"\033]112\a\033[0 q")

    def test_terminal_restore_tolerates_a_closed_tmux_pane(self):
        shell = self._make_shell("auto")
        shell.old_tty_attrs = object()
        shell._visual_mode = "cursor"

        with (
            patch("sys.stdout") as stdout,
            patch("termios.tcsetattr", side_effect=termios.error(5, "Input/output error")),
        ):
            stdout.buffer.write.side_effect = BrokenPipeError
            shell._restore_terminal()


class TestHeadlessImport(unittest.TestCase):
    def test_main_imports_without_inquirerpy(self):
        import importlib

        import vsh.core.config as cfg_mod

        importlib.reload(cfg_mod)
        self.assertTrue(hasattr(cfg_mod, "VshConfig"))

    def test_voice_bridge_environment_overrides_config(self):
        with tempfile.TemporaryDirectory() as config_home:
            config_dir = Path(config_home) / "vsh"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text("[shell]\n")
            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": config_home,
                    "VSH_VOICE_HANDLER": "voice-tool {}",
                    "VSH_RESPONSE_BRIDGE": "fish-signal",
                    "VSH_OUTPUT_MODE": "speak_and_command",
                },
            ):
                config = load_config()

        self.assertEqual(config.shell.voice_handler, "voice-tool {}")
        self.assertEqual(config.shell.response_bridge, "fish-signal")
        self.assertEqual(config.llm.output_mode, "speak_and_command")


if __name__ == "__main__":
    unittest.main()
