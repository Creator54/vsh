from unittest.mock import MagicMock, patch

import pytest

from vsh.core.voice_indicator import CURSOR_RESET, VoiceIndicator
from vsh.core.voice_input import VoiceState


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, CURSOR_RESET),
        (VoiceState.MUTED, b"\033]12;#ff8232\a\033[4 q"),
        (VoiceState.IDLE, b"\033]12;#00d2ff\a\033[6 q"),
        (VoiceState.LISTENING, b"\033]12;#ff00b4\a\033[1 q"),
        (VoiceState.TRANSCRIBING, b"\033]12;#00d2ff\a\033[3 q"),
        (VoiceState.THINKING, b"\033]12;#ffae00\a\033[4 q"),
        (VoiceState.TYPING, b"\033]12;#32dc64\a\033[6 q"),
        (VoiceState.SPEAKING, b"\033]12;#32dc64\a\033[5 q"),
    ],
)
def test_cursor_sequences_remain_exact(state, expected):
    indicator = VoiceIndicator("auto", 1000)
    indicator.mode = "cursor"
    output = MagicMock()

    with patch("sys.stdout") as stdout:
        stdout.buffer = output
        indicator.render(state)

    output.write.assert_called_once_with(expected)


def test_tmux_graphics_passthrough_remains_exact():
    sequence = b"\033_Gi=1;AAAA\033\\"

    with patch.dict("os.environ", {"TMUX": "/tmp/tmux"}):
        wrapped = VoiceIndicator._terminal_sequence(sequence)

    assert wrapped == b"\033Ptmux;\033\033_Gi=1;AAAA\033\033\\\033\\"
