import os
from pathlib import Path
from unittest.mock import patch

from vsh.core.config import load_config


def test_load_config_preserves_sections_profiles_and_environment_precedence(tmp_path: Path):
    config_dir = tmp_path / "vsh"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[shell]
inner_shell = "/bin/fish"
voice_on_start = false
overlay_mode = "kitty"
response_bridge = "fish-signal"

[keybinds]
toggle_listen = "ctrl+]"
toggle_listen_triggers = ["1d"]

[stt]
provider = "custom_http"
endpoint = "https://speech.example"
api_key_env = "STT_TEST_KEY"
vad_threshold = 2345

[tts]
provider = "none"

[llm]
provider = "saved"
output_mode = "command_only"

[llm.saved]
type = "cli"
command = "assistant --stdin"
api_key_env = "LLM_TEST_KEY"
"""
    )
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path),
        "STT_TEST_KEY": "speech-secret",
        "LLM_TEST_KEY": "llm-secret",
        "VSH_VOICE": "true",
        "VSH_OUTPUT_MODE": "speak_only",
    }

    with patch.dict(os.environ, environment, clear=True):
        config = load_config()

    assert config.shell.inner_shell == "/bin/fish"
    assert config.shell.voice_on_start is True
    assert config.shell.overlay_mode == "kitty"
    assert config.keybinds.toggle_listen_triggers == ["1d"]
    assert config.stt.api_key == "speech-secret"
    assert config.stt.vad_threshold == 2345
    assert config.llm.output_mode == "speak_only"
    assert config.custom_thinkers["saved"] == {
        "type": "cli",
        "command": "assistant --stdin",
        "api_key_env": "LLM_TEST_KEY",
        "api_key": "llm-secret",
    }
