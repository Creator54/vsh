import os
import struct
import tempfile
import termios
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import vsh.core.pty_shell as pty_module
from vsh.core.config import VshConfig, load_config
from vsh.core.pty_shell import PtyShell
from vsh.core.voice_indicator import CURSOR_RESET
from vsh.core.voice_input import VoiceState


class _FakeVoiceThread:
    def __init__(self, state_callback=None, capture_started_callback=None, capture_stopped_callback=None):
        self.is_listening = False
        self.is_processing = False
        self.system_mic_muted = None
        self.state_callback = state_callback
        self.capture_started_callback = capture_started_callback
        self.capture_stopped_callback = capture_stopped_callback

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
        cfg.tts.provider = "none"
        with patch(
            "vsh.core.pty_shell.VoiceInputThread",
            side_effect=lambda *_args, **kwargs: _FakeVoiceThread(
                kwargs.get("state_callback"),
                kwargs.get("capture_started_callback"),
                kwargs.get("capture_stopped_callback"),
            ),
        ):
            shell = PtyShell(cfg, thinker=None, verbose=False, tts_provider=None)
        shell.master_fd = 7
        shell.indicator.resize(80)
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
        with patch.object(shell.indicator, "_probe_graphics_support", return_value=False):
            shell.indicator.select_mode(True)
        self.assertEqual(shell.indicator.mode, "cursor")

    def test_probe_accepts_graphics_response_before_device_attributes(self):
        shell = self._make_shell("auto")
        shell.old_tty_attrs = object()
        response = f"\x1b_Gi={shell.indicator._image_id};OK\x1b\\\x1b[?1;2cuser".encode()
        with (
            patch("sys.stdout") as stdout,
            patch("sys.stdin.fileno", return_value=0),
            patch("select.select", return_value=([0], [], [])),
            patch("os.read", return_value=response),
        ):
            stdout.buffer = MagicMock()
            self.assertTrue(shell.indicator._probe_graphics_support(timeout=0.25))
        self.assertEqual(shell.indicator.pending_input, b"user")

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
            self.assertFalse(shell.indicator._probe_graphics_support(timeout=0.25))

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
        shell.indicator.mode = "graphics"
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell.indicator.render(shell._effective_cursor_state())
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
        shell.indicator.mode = "graphics"
        shell.indicator._graphics_visible = True
        buf = MagicMock()

        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell.voice_thread.is_listening = False
            shell._current_cursor_state = None
            shell.indicator.render(shell._effective_cursor_state())

        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"a=d,d=I", written)
        self.assertNotIn(b"a=t", written)
        self.assertNotIn(b"a=p", written)
        self.assertIn(b"\033[?25h", written)
        self.assertFalse(shell.indicator._graphics_visible)

    def test_graphics_redraw_deletes_the_previous_frame_first(self):
        shell = self._make_shell("kitty")
        shell.indicator.mode = "graphics"
        buf = MagicMock()

        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell._set_system_mic_muted(True)
            shell.indicator.render(shell._effective_cursor_state())
            buf.reset_mock()
            shell._set_system_mic_muted(False)
            shell.indicator.render(shell._effective_cursor_state())

        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"a=d,d=I", written)
        self.assertLess(written.index(b"a=d,d=I"), written.index(b"a=t"))

    def test_typing_restores_native_cursor_and_delays_image(self):
        shell = self._make_shell("kitty")
        shell.indicator.mode = "graphics"
        shell.indicator._graphics_visible = True
        buf = MagicMock()
        with patch("sys.stdout") as stdout, patch("time.monotonic", return_value=10.0):
            stdout.buffer = buf
            shell._user_started_typing()
        written = b"".join(call.args[0] for call in buf.write.call_args_list)
        self.assertIn(b"a=d,d=I", written)
        self.assertIn(b"\033[?25h", written)
        self.assertGreater(shell.indicator._typing_until, 10.0)
        self.assertEqual(shell._current_cursor_state, VoiceState.IDLE)

    def test_typing_temporarily_suppresses_microphone_input(self):
        shell = self._make_shell("none")
        shell.voice_thread.suppress_input = MagicMock()

        with patch("time.monotonic", return_value=10.0):
            shell._user_started_typing()

        shell.voice_thread.suppress_input.assert_called_once_with(0.6)

    def test_graphics_cursor_suppressed_in_alternate_screen(self):
        shell = self._make_shell("kitty")
        shell.indicator.mode = "graphics"
        shell.indicator._alternate_screen = True
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell.indicator.render(shell._effective_cursor_state())
        buf.write.assert_not_called()

    def test_cursor_fallback_writes_only_cursor_controls(self):
        shell = self._make_shell("auto")
        shell.indicator.mode = "cursor"
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
        self.assertEqual(shell.indicator.energy, 9000)

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

    def test_voice_status_uses_the_same_effective_state_as_the_indicator(self):
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
        dispatch.assert_called_once_with('{"speech":"Ready.","command":"pwd"}', 0)
        inject.assert_not_called()

    def test_pipeline_discards_queued_work_after_shutdown_starts(self):
        shell = self._make_shell("none")
        shell.thinker = MagicMock()
        shell.stt_queue.put("do not run")
        shell.stt_queue.put(None)
        shell._exiting = True

        shell._pipeline_worker()

        shell.thinker.ask.assert_not_called()

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
        self.assertIn("shell command string or null", prompt)
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

    def test_tts_is_loaded_only_when_speech_is_requested(self):
        shell = self._make_shell("none")
        shell.config.tts.provider = "supertonic"
        provider = object()

        with patch("vsh.providers.resolve_tts", return_value=provider) as resolve:
            self.assertIs(shell._load_tts(), provider)
            self.assertIs(shell._load_tts(), provider)

        resolve.assert_called_once_with(shell.config)

    def test_voice_command_is_skipped_after_keyboard_input_changes(self):
        shell = self._make_shell("none")
        shell.shell_pid = 4321
        shell._input_revision = 2

        with patch("os.tcgetpgrp", return_value=4321), patch("os.write") as write:
            self.assertFalse(shell._inject_command("pwd", expected_revision=1))

        write.assert_not_called()

    def test_voice_command_is_skipped_while_foreground_program_owns_pty(self):
        shell = self._make_shell("none")
        shell.shell_pid = 4321

        with patch("os.tcgetpgrp", return_value=9876), patch("os.write") as write:
            self.assertFalse(shell._inject_command("pwd", expected_revision=0))

        write.assert_not_called()

    def test_prompt_marker_cannot_override_foreground_pty_ownership(self):
        shell = self._make_shell("none")
        shell.shell_pid = 4321
        shell._track_pty_controls(b"\x1b]133;A\x07")

        with patch("os.tcgetpgrp", return_value=9876), patch("os.write") as write:
            self.assertFalse(shell._inject_command("pwd", expected_revision=0))

        write.assert_not_called()

    def test_voice_command_requires_a_prompt_marker_for_every_shell(self):
        shell = self._make_shell("none")
        shell.shell_name = "bash"
        shell.shell_pid = 4321

        with patch("sys.stdout") as stdout, patch("os.tcgetpgrp", return_value=4321), patch("os.write") as write:
            self.assertFalse(shell._inject_command("pwd", expected_revision=0))

        write.assert_not_called()
        stdout.write.assert_called_once_with('\r\nvsh: command not injected: "pwd"\r\n')

    def test_voice_command_is_written_at_an_observed_clean_prompt(self):
        shell = self._make_shell("none")
        shell.shell_name = "bash"
        shell.shell_pid = 4321
        shell._track_pty_controls(b"\x1b]133;A\x07")

        with patch("os.tcgetpgrp", return_value=4321), patch("os.write") as write:
            self.assertTrue(shell._inject_command("pwd", expected_revision=0))

        write.assert_called_once_with(7, b"pwd")

    def test_fish_voice_command_requires_a_clean_osc133_prompt(self):
        shell = self._make_shell("none")
        shell.shell_name = "fish"
        shell.shell_pid = 4321

        with patch("os.tcgetpgrp", return_value=4321), patch("os.write") as write:
            self.assertFalse(shell._inject_command("pwd", expected_revision=0))

            shell._track_pty_controls(b"\x1b]13")
            shell._track_pty_controls(b"3;A;fish=4\x07")
            self.assertFalse(shell._inject_command("pwd", expected_revision=0))
            shell._track_pty_controls(b"\x1b]777;vsh-")
            shell._track_pty_controls(b"prompt\x07")
            self.assertTrue(shell._inject_command("pwd", expected_revision=0))

            shell._input_revision = 1
            self.assertFalse(shell._inject_command("pwd", expected_revision=1))

            shell._track_pty_controls(b"\x1b]133;C\x07")
            self.assertFalse(shell._inject_command("pwd", expected_revision=1))
            shell._track_pty_controls(b"\x1b]133;D;0\x07")
            self.assertFalse(shell._inject_command("pwd", expected_revision=1))
            shell._track_pty_controls(b"\x1b]133;A\x07")
            self.assertFalse(shell._inject_command("pwd", expected_revision=1))
            shell._track_pty_controls(b"\x1b]777;vsh-prompt\x07")
            self.assertTrue(shell._inject_command("pwd", expected_revision=1))

        self.assertEqual(write.call_count, 2)

    def test_fish_exec_installs_a_main_prompt_marker(self):
        shell = self._make_shell("none")
        shell.shell_name = "fish"
        shell.inner_shell_args = ["/bin/fish", "--no-config", "--init-command", "set user_init 1", "-c", "echo ok"]

        args = shell._shell_exec_args()

        self.assertEqual(args[0], "/bin/fish")
        self.assertEqual(args[:4], ["/bin/fish", "--no-config", "--init-command", "set user_init 1"])
        self.assertEqual(args[4], "--init-command")
        self.assertIn("__vsh_saved_fish_prompt", args[5])
        self.assertEqual(args[6:], ["-c", "echo ok"])

    def test_shutdown_cancels_cli_processors(self):
        shell = self._make_shell("none")
        shell.thinker = MagicMock()
        shell.voice_handler = MagicMock()

        shell._cancel_processors()

        shell.thinker.cancel.assert_called_once_with()
        shell.voice_handler.cancel.assert_called_once_with()

    def test_shutdown_continues_cancelling_after_one_processor_fails(self):
        shell = self._make_shell("none")
        shell.voice_handler = MagicMock()
        shell.voice_handler.cancel.side_effect = PermissionError("denied")
        shell.thinker = MagicMock()

        shell._cancel_processors()

        shell.voice_handler.cancel.assert_called_once_with()
        shell.thinker.cancel.assert_called_once_with()

    def test_late_reply_is_dropped_after_terminal_restoration_starts(self):
        shell = self._make_shell("none")
        started = threading.Event()

        def publish():
            started.set()
            shell._publish_reply("too late", "")

        with patch("sys.stdout") as stdout:
            shell._terminal_lock.acquire()
            worker = threading.Thread(target=publish)
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            shell._exiting = True
            shell._terminal_lock.release()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        stdout.write.assert_not_called()

    def test_pipewire_monitor_exists_only_while_capture_is_owned(self):
        shell = self._make_shell("none")
        monitor = MagicMock()
        monitor.is_alive.return_value = False

        with patch("vsh.core.pty_shell.PipeWireMicMonitor", return_value=monitor) as constructor:
            self.assertIsNone(shell.mic_monitor)
            shell.voice_thread.capture_started_callback()
            constructor.assert_called_once()
            monitor.start.assert_called_once_with()
            shell.voice_thread.capture_stopped_callback()

        monitor.stop.assert_called_once_with()
        monitor.join.assert_called_once()
        self.assertIsNone(shell.mic_monitor)

    def test_stopping_monitor_does_not_block_a_replacement_if_join_times_out(self):
        shell = self._make_shell("none")
        old_monitor = MagicMock()
        old_monitor.is_alive.return_value = True
        new_monitor = MagicMock()

        shell.mic_monitor = old_monitor
        with patch("vsh.core.pty_shell.PipeWireMicMonitor", return_value=new_monitor) as constructor:
            shell._stop_mic_monitor()
            shell._start_mic_monitor()

        old_monitor.stop.assert_called_once_with()
        old_monitor.join.assert_called_once_with(timeout=2)
        constructor.assert_called_once()
        new_monitor.start.assert_called_once_with()
        self.assertIs(shell.mic_monitor, new_monitor)

    def test_replaced_monitor_cannot_publish_stale_mute_state(self):
        shell = self._make_shell("none")
        callbacks = []

        def make_monitor(callback):
            callbacks.append(callback)
            return MagicMock()

        with patch("vsh.core.pty_shell.PipeWireMicMonitor", side_effect=make_monitor):
            shell._start_mic_monitor()
            shell._stop_mic_monitor()
            shell._start_mic_monitor()

        callbacks[0](True)
        self.assertIsNone(shell.voice_status()["mic_muted"])
        callbacks[1](False)
        self.assertFalse(shell.voice_status()["mic_muted"])

    def test_deprecated_fish_bridge_uses_stdout_and_pty_without_signalling(self):
        shell = self._make_shell("none")
        shell.config.shell.response_bridge = "fish-signal"
        shell.config.shell.auto_submit = True
        shell.shell_name = "fish"
        shell.shell_pid = 4321

        with patch("sys.stdout") as stdout, patch("os.write") as write, patch("os.kill") as kill:
            shell._publish_reply("Voice response", "pwd")

        stdout.write.assert_called_once_with("\r\nVoice response\r\n")
        write.assert_called_once_with(7, b"pwd\n")
        kill.assert_not_called()

    def test_pty_reply_leaves_command_editable_when_auto_submit_is_off(self):
        shell = self._make_shell("none")
        shell.config.shell.auto_submit = False
        shell.shell_name = "fish"
        shell.shell_pid = 4321

        with patch("os.write") as write:
            shell._publish_reply("", "cd /tmp")

        write.assert_called_once_with(7, b"cd /tmp")

    def test_graphics_cleanup_is_targeted(self):
        shell = self._make_shell("kitty")
        shell.indicator._graphics_visible = True
        buf = MagicMock()
        with patch("sys.stdout") as stdout:
            stdout.buffer = buf
            shell.indicator._delete_graphics()
        written = buf.write.call_args[0][0]
        self.assertIn(b"a=d,d=I", written)
        self.assertNotIn(b"a=d,d=a", written)

    def test_none_mode_is_visual_noop(self):
        shell = self._make_shell("none")
        shell.old_tty_attrs = object()
        shell.indicator.select_mode(True)
        self.assertEqual(shell.indicator.mode, "none")
        self.assertEqual(CURSOR_RESET, b"\033]112\a\033[0 q")

    def test_terminal_restore_tolerates_a_closed_tmux_pane(self):
        shell = self._make_shell("auto")
        shell.old_tty_attrs = object()
        shell.indicator.mode = "cursor"

        with (
            patch("sys.stdout") as stdout,
            patch("termios.tcsetattr", side_effect=termios.error(5, "Input/output error")),
        ):
            stdout.buffer.write.side_effect = BrokenPipeError
            shell._restore_terminal()

    def test_late_worker_state_cannot_render_during_shutdown(self):
        shell = self._make_shell("kitty")
        shell._exiting = True

        with (
            patch.object(shell.indicator, "render") as render,
            patch.object(shell.voice_thread, "set_processing") as set_processing,
        ):
            shell._set_cursor_state(VoiceState.THINKING)
            shell._set_system_mic_muted(True)

        render.assert_not_called()
        set_processing.assert_not_called()

    def test_output_snapshot_is_a_locked_copy(self):
        shell = self._make_shell("none")
        shell.output_history.extend((b"one", b"two"))

        self.assertEqual(shell.output_snapshot(), b"onetwo")

    def test_reap_shell_waits_after_terminating_the_process_group(self):
        shell = self._make_shell("none")
        shell.shell_pid = 4321

        with (
            patch("os.waitpid", side_effect=[(0, 0), (4321, 0)]) as waitpid,
            patch("os.getpgid", return_value=4321),
            patch("os.killpg") as killpg,
        ):
            shell._reap_shell()

        self.assertEqual(waitpid.call_args_list[0].args, (4321, os.WNOHANG))
        self.assertEqual(waitpid.call_args_list[1].args, (4321, 0))
        killpg.assert_called_once_with(4321, pty_module.signal.SIGKILL)

    def test_parent_startup_failure_still_restores_and_reaps(self):
        shell = self._make_shell("none")
        shell._restore_terminal = MagicMock()
        shell._reap_shell = MagicMock()

        with (
            patch("vsh.core.pty_shell.pty.fork", return_value=(4321, 7)),
            patch("vsh.core.pty_shell.signal.signal"),
            patch.object(shell, "_handle_sigwinch"),
            patch.object(shell, "_setup_terminal"),
            patch.object(shell.indicator, "select_mode", side_effect=RuntimeError("probe failed")),
            patch("vsh.core.pty_shell.os.tcgetpgrp", side_effect=OSError),
            patch("vsh.core.pty_shell.os.close") as close,
        ):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                shell.run()

        self.assertTrue(shell._exiting)
        shell._restore_terminal.assert_called_once_with()
        close.assert_called_once_with(7)
        shell._reap_shell.assert_called_once_with()

    def test_cleanup_kills_a_foreground_group_owned_by_the_shell_session(self):
        shell = self._make_shell("none")
        shell.shell_pid = 4321

        with (
            patch("os.tcgetpgrp", return_value=9876),
            patch("os.killpg") as killpg,
        ):
            shell._kill_foreground_process_group()

        killpg.assert_called_once_with(9876, pty_module.signal.SIGKILL)

    def test_cleanup_kills_a_foreground_group_after_its_leader_exits(self):
        shell = self._make_shell("none")
        shell.shell_pid = 4321

        with (
            patch("os.tcgetpgrp", return_value=9876),
            patch("os.getsid", side_effect=ProcessLookupError),
            patch("os.killpg") as killpg,
        ):
            shell._kill_foreground_process_group()

        killpg.assert_called_once_with(9876, pty_module.signal.SIGKILL)


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
