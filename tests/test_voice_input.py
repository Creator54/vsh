from vsh.core.voice_input import _is_silence_hallucination


def test_rejects_known_whisper_silence_hallucination():
    assert _is_silence_hallucination("Subtitles by the Amara.org community")
    assert _is_silence_hallucination("THANK YOU FOR WATCHING!")


def test_keeps_real_voice_requests():
    assert not _is_silence_hallucination("Open the project in my editor")
