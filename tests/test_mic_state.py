from unittest.mock import patch

from vsh.core.mic_state import JsonStreamDecoder, PipeWireMicMonitor, PipeWireState


def _source(source_id, name, muted):
    return {
        "id": source_id,
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {
                "media.class": "Audio/Source",
                "node.name": name,
            },
            "params": {"Props": [{"mute": muted}]},
        },
    }


def _default_source(name):
    return {
        "id": 41,
        "type": "PipeWire:Interface:Metadata",
        "metadata": [
            {
                "subject": 0,
                "key": "default.audio.source",
                "type": "Spa:String:JSON",
                "value": {"name": name},
            }
        ],
    }


def test_pipewire_state_resolves_the_default_audio_source():
    state = PipeWireState()

    muted = state.update(
        [
            _source(10, "other", False),
            _source(11, "default", True),
            _default_source("default"),
        ]
    )

    assert muted is True


def test_pipewire_state_tracks_param_only_mute_updates():
    state = PipeWireState()
    state.update([_source(11, "default", False), _default_source("default")])

    muted = state.update(
        [
            {
                "id": 11,
                "type": "PipeWire:Interface:Node",
                "info": {"params": {"Props": [{"mute": True}]}},
            }
        ]
    )

    assert muted is True


def test_pipewire_state_tracks_default_source_changes():
    state = PipeWireState()
    state.update(
        [
            _source(11, "built-in", False),
            _source(12, "headset", True),
            _default_source("built-in"),
        ]
    )

    muted = state.update([_default_source("headset")])

    assert muted is True


def test_pipewire_state_returns_unknown_without_a_default_source():
    state = PipeWireState()

    assert state.update([_source(11, "built-in", False)]) is None


def test_json_stream_decoder_handles_split_pipewire_documents():
    decoder = JsonStreamDecoder()

    assert decoder.feed('[{"id": 1') == []
    assert decoder.feed('}]\n[{"id": 2}]\n') == [[{"id": 1}], [{"id": 2}]]


def test_missing_pipewire_monitor_reports_unknown_without_failing():
    states = []
    with patch("vsh.core.mic_state.shutil.which", return_value=None):
        monitor = PipeWireMicMonitor(states.append)

    monitor.run()

    assert not monitor.available
    assert states == [None]
