import queue

from vsh.core.audio import PhraseCapture
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


def make_thread(provider, states):
    thread = VoiceInputThread(queue.Queue(), state_callback=states.append)
    thread.stt_provider = provider
    thread.model_loaded = True
    thread.is_listening = True
    return thread


def test_toggle_does_not_load_the_model_on_the_terminal_thread():
    states = []
    thread = VoiceInputThread(queue.Queue(), state_callback=states.append)
    thread.load_model = lambda: (_ for _ in ()).throw(AssertionError("loaded synchronously"))

    assert thread.toggle_listening()
    assert states == [VoiceState.IDLE]


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
