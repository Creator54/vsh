import multiprocessing
import os
import queue
import threading
import time
from pathlib import Path
from unittest.mock import patch

from vsh.core.audio import PhraseCapture
from vsh.core.microphone import acquire_microphone, release_microphone
from vsh.core.voice_input import VoiceInputThread, VoiceState, _is_silence_hallucination, _is_valid_transcript


class FakeStream:
    def __init__(self, capture, activity=False):
        self.capture = capture
        self.activity = activity

    def capture_phrase(self, **kwargs):
        if self.activity:
            kwargs["activity_callback"](True)
            kwargs["activity_callback"](False)
        return self.capture


class FakeControllableStream:
    def __init__(self):
        self.events = []

    def suspend(self):
        self.events.append("suspend")

    def resume(self):
        self.events.append("resume")


class FakeProvider:
    def __init__(self, text, before_return=None):
        self.text = text
        self.before_return = before_return
        self.calls = []

    def transcribe_stream(self, chunks):
        self.calls.append(tuple(chunks))
        if self.before_return:
            self.before_return()
        return self.text


def _acquire_microphone_in_process(runtime_dir: str, result) -> None:
    os.environ["XDG_RUNTIME_DIR"] = runtime_dir
    lease = acquire_microphone()
    result.put(lease is not None)
    release_microphone(lease)


def make_thread(provider, states):
    thread = VoiceInputThread(queue.Queue(), state_callback=states.append)
    thread.stt_provider = provider
    thread.model_loaded = True
    thread.is_listening = True
    return thread


def test_toggle_does_not_load_the_model_on_the_terminal_thread(tmp_path: Path):
    states = []
    thread = VoiceInputThread(queue.Queue(), state_callback=states.append)
    thread.load_model = lambda: (_ for _ in ()).throw(AssertionError("loaded synchronously"))

    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(tmp_path)}):
        assert thread.toggle_listening()
        assert states == [VoiceState.IDLE]
        thread.stop()


def test_concurrent_toggles_publish_callbacks_in_transition_order():
    first_callback_entered = threading.Event()
    release_first_callback = threading.Event()
    states = []

    def state_changed(state):
        if state == VoiceState.IDLE:
            first_callback_entered.set()
            assert release_first_callback.wait(timeout=1)
        states.append(state)

    voice = VoiceInputThread(queue.Queue(), state_callback=state_changed)
    first = threading.Thread(target=voice.toggle_listening)
    second = threading.Thread(target=voice.toggle_listening)
    first.start()
    assert first_callback_entered.wait(timeout=1)
    second.start()
    second_finished_early = not second.is_alive()
    release_first_callback.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not second_finished_early
    assert states == [VoiceState.IDLE, None]
    assert not voice.is_listening


def test_concurrent_suppression_keeps_the_longer_deadline():
    voice = VoiceInputThread(queue.Queue())
    long_update_done = threading.Event()

    def clock():
        if threading.current_thread().name == "short-suppression":
            assert long_update_done.wait(timeout=1)
        return 10.0

    def suppress_longer():
        voice.suppress_input(0.6)
        long_update_done.set()

    with patch("vsh.core.voice_input.time.monotonic", side_effect=clock):
        short = threading.Thread(name="short-suppression", target=voice.suppress_input, args=(0.3,))
        long = threading.Thread(name="long-suppression", target=suppress_longer)
        short.start()
        long.start()
        short.join(timeout=1)
        long.join(timeout=1)

    assert voice._suppress_until == 10.6


def test_microphone_lease_is_exclusive_and_released(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(tmp_path)}):
        lease = acquire_microphone()
        assert lease is not None
        process = context.Process(target=_acquire_microphone_in_process, args=(str(tmp_path), result))
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert result.get(timeout=1) is False
        release_microphone(lease)

        process = context.Process(target=_acquire_microphone_in_process, args=(str(tmp_path), result))
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert result.get(timeout=1) is True


def test_microphone_lease_is_held_until_stream_close_finishes(tmp_path: Path):
    entered = threading.Event()
    closing = threading.Event()
    finish_close = threading.Event()

    class ClosingStream:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            entered.set()
            return self

        def __exit__(self, *_args):
            closing.set()
            assert finish_close.wait(timeout=2)

        def suspend(self):
            pass

        def abort(self):
            pass

    thread = VoiceInputThread(queue.Queue())
    thread.is_listening = True
    thread.is_processing = True
    thread.load_model = lambda: None

    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(tmp_path)}), patch("vsh.core.audio.MicStream", ClosingStream):
        thread.start()
        assert entered.wait(timeout=1)
        assert not thread.toggle_listening()
        assert closing.wait(timeout=1)
        assert acquire_microphone() is None
        finish_close.set()
        thread.stop()
        thread.join(timeout=2)
        lease = acquire_microphone()

    assert not thread.is_alive()
    assert lease is not None
    release_microphone(lease)


def test_model_load_precedes_lease_and_monitor_lifetime():
    events = []
    lease = object()

    class OneShotStream:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            events.append("stream-enter")
            return self

        def __exit__(self, *_args):
            events.append("stream-exit")

        def suspend(self):
            pass

    thread = VoiceInputThread(
        queue.Queue(),
        capture_started_callback=lambda: events.append("monitor-start"),
        capture_stopped_callback=lambda: events.append("monitor-stop"),
    )
    thread.is_listening = True
    thread.is_processing = True
    thread.load_model = lambda: events.append("model")

    def acquire():
        events.append("lease-acquire")
        return lease

    def release(value):
        assert value is lease
        events.append("lease-release")

    def stop_after_enter():
        while "stream-enter" not in events:
            time.sleep(0.001)
        thread.should_exit = True

    stopper = threading.Thread(target=stop_after_enter)
    stopper.start()
    with (
        patch("vsh.core.microphone.acquire_microphone", side_effect=acquire),
        patch("vsh.core.microphone.release_microphone", side_effect=release),
        patch("vsh.core.audio.MicStream", OneShotStream),
    ):
        thread.run()
    stopper.join(timeout=1)

    assert events == [
        "model",
        "lease-acquire",
        "stream-enter",
        "monitor-start",
        "stream-exit",
        "monitor-stop",
        "lease-release",
    ]


def test_system_unmute_resumes_capture_without_releasing_monitor_or_lease():
    events = []
    capture_started = threading.Event()
    monitor_started = threading.Event()
    monitor_stopped = threading.Event()
    lease = object()

    class MutedStream:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            events.append("stream-enter")
            return self

        def __exit__(self, *_args):
            events.append("stream-exit")

        def suspend(self):
            events.append("stream-suspend")

        def resume(self):
            events.append("stream-resume")

        def abort(self):
            pass

        def capture_phrase(self, **_kwargs):
            events.append("capture")
            capture_started.set()
            thread.should_exit = True
            return PhraseCapture()

    def monitor_start():
        events.append("monitor-start")
        thread.set_system_mic_muted(True)
        monitor_started.set()

    def monitor_stop():
        events.append("monitor-stop")
        monitor_stopped.set()

    thread = VoiceInputThread(
        queue.Queue(),
        capture_started_callback=monitor_start,
        capture_stopped_callback=monitor_stop,
    )
    thread.is_listening = True
    thread.load_model = lambda: events.append("model")

    with (
        patch("vsh.core.microphone.acquire_microphone", side_effect=lambda: events.append("lease-acquire") or lease),
        patch("vsh.core.microphone.release_microphone", side_effect=lambda _lease: events.append("lease-release")),
        patch("vsh.core.audio.MicStream", MutedStream),
    ):
        thread.start()
        assert monitor_started.wait(timeout=1)
        stopped_while_muted = monitor_stopped.wait(timeout=0.1)
        thread.set_system_mic_muted(False)
        assert capture_started.wait(timeout=1)
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert not stopped_while_muted
    assert events == [
        "model",
        "lease-acquire",
        "stream-enter",
        "monitor-start",
        "stream-suspend",
        "stream-resume",
        "capture",
        "stream-exit",
        "monitor-stop",
        "lease-release",
    ]


def test_processing_state_suspends_and_resumes_the_active_stream():
    thread = VoiceInputThread(queue.Queue())
    stream = FakeControllableStream()
    thread._set_active_stream(stream)

    thread.set_processing(True)
    thread.set_processing(False)

    assert stream.events == ["suspend", "resume"]
    assert not thread.is_processing


def test_rejects_known_whisper_silence_hallucination():
    assert _is_silence_hallucination("Subtitles by the Amara.org community")
    assert _is_silence_hallucination("THANK YOU FOR WATCHING!")


def test_keeps_real_voice_requests():
    assert not _is_silence_hallucination("Open the project in my editor")


def test_rejects_empty_and_punctuation_only_transcripts():
    assert not _is_valid_transcript("")
    assert not _is_valid_transcript("... ?!")
    assert not _is_valid_transcript("Subtitles by the Amara.org community")
    assert _is_valid_transcript("list files")


def test_confirmed_phrase_enters_listening_then_transcribing():
    states = []
    provider = FakeProvider("list files")
    thread = make_thread(provider, states)
    stream = FakeStream(PhraseCapture((b"speech",), 120, "silence", True), activity=True)

    queued = thread.process_once(stream)

    assert queued
    assert thread.stt_queue.get_nowait() == "list files"
    assert states == [VoiceState.LISTENING, VoiceState.TRANSCRIBING]
    assert provider.calls == [(b"speech",)]


def test_rejected_capture_returns_to_idle_without_stt():
    states = []
    provider = FakeProvider("should not run")
    thread = make_thread(provider, states)

    queued = thread.process_once(FakeStream(PhraseCapture()))

    assert not queued
    assert states == [VoiceState.IDLE]
    assert provider.calls == []


def test_system_mute_stops_capture_without_disabling_vsh():
    states = []
    provider = FakeProvider("should not run")
    thread = make_thread(provider, states)
    stream = FakeStream(PhraseCapture((b"speech",), 120, "silence", True))

    thread.set_system_mic_muted(True)
    queued = thread.process_once(stream)

    assert thread.is_listening
    assert not queued
    assert provider.calls == []
    assert states == [VoiceState.IDLE]


def test_system_mute_during_stt_discards_the_result():
    states = []
    thread = make_thread(None, states)
    provider = FakeProvider("list files", before_return=lambda: thread.set_system_mic_muted(True))
    thread.stt_provider = provider

    queued = thread.process_once(FakeStream(PhraseCapture((b"speech",), 120, "silence", True)))

    assert not queued
    assert thread.stt_queue.empty()
    assert thread.is_listening
    assert states == [VoiceState.TRANSCRIBING, VoiceState.IDLE]


def test_toggle_off_during_stt_discards_the_result():
    states = []
    thread = make_thread(None, states)
    provider = FakeProvider("list files", before_return=lambda: setattr(thread, "is_listening", False))
    thread.stt_provider = provider

    queued = thread.process_once(FakeStream(PhraseCapture((b"speech",), 120, "silence", True)))

    assert not queued
    assert thread.stt_queue.empty()
    assert states == [VoiceState.TRANSCRIBING, None]
    assert not thread.is_processing


def test_hallucination_returns_to_idle():
    states = []
    provider = FakeProvider("Subtitles by the Amara.org community")
    thread = make_thread(provider, states)

    queued = thread.process_once(FakeStream(PhraseCapture((b"speech",), 120, "silence", True)))

    assert not queued
    assert states == [VoiceState.TRANSCRIBING, VoiceState.IDLE]
    assert not thread.is_processing


def test_voice_thread_is_daemon_and_stops_cleanly():
    thread = VoiceInputThread(queue.Queue())
    assert thread.daemon
    thread.stop()
    assert thread.should_exit
    assert not thread.is_listening
