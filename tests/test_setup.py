import os
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

from vsh.core.setup import get_default_rc, interactive_setup, update_keybind_config, update_shell_rc_bind


def test_keybind_update_preserves_other_tables_and_values(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '# keep this comment\n[shell]\ninner_shell = "/bin/fish"\n\n'
        '[keybinds]\ntoggle_listen = "ctrl+g"\ncustom = true\n\n'
        '[stt]\nprovider = "vosk"\n'
    )

    update_keybind_config(config, {"name": "ctrl+]", "triggers": ["1d", "1b5b39333b3575"]})

    data = tomllib.loads(config.read_text())
    assert data["shell"] == {"inner_shell": "/bin/fish"}
    assert data["keybinds"] == {
        "toggle_listen": "ctrl+]",
        "custom": True,
        "toggle_listen_triggers": ["1d", "1b5b39333b3575"],
    }
    assert data["stt"] == {"provider": "vosk"}
    assert config.read_text().startswith("# keep this comment\n[shell]")


def test_keybind_update_adds_a_missing_table(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[shell]\ninner_shell = "/bin/bash"\n')

    update_keybind_config(config, {"name": "ctrl+g", "triggers": ["07"]})

    data = tomllib.loads(config.read_text())
    assert data["keybinds"] == {"toggle_listen": "ctrl+g", "toggle_listen_triggers": ["07"]}


def test_fish_shell_update_replaces_one_managed_block(tmp_path: Path):
    config = tmp_path / "config.fish"
    config.write_text(
        "set -gx EDITOR nvim\n# --- vsh configuration start ---\nold command\n# --- vsh configuration end ---\n"
    )
    keybind = {"name": "ctrl+]", "fish": "\\c]"}

    with patch("sys.stdout"):
        assert update_shell_rc_bind(str(config), keybind, True)

    content = config.read_text()
    assert content.startswith("set -gx EDITOR nvim\n")
    assert content.count("# --- vsh configuration start ---") == 1
    assert "bind \\c] 'vsh --voice; commandline -f repaint'" in content
    assert 'test "$VSH_ACTIVE_TTY" != (tty)' in content


def test_shell_update_prepends_managed_block_when_setting_default(tmp_path: Path):
    config = tmp_path / "config.fish"
    config.write_text("set -gx EDITOR nvim\n")

    with patch("sys.stdout"):
        assert update_shell_rc_bind(str(config), None, True)

    content = config.read_text()
    assert content.startswith("# --- vsh configuration start ---")
    assert 'test "$VSH_ACTIVE_TTY" != (tty)' in content
    assert "set -gx EDITOR nvim\n" in content


def test_get_default_rc():
    assert get_default_rc("/usr/bin/fish") == "~/.config/fish/config.fish"
    assert get_default_rc("/bin/zsh") == "~/.zshrc"
    assert get_default_rc("/usr/bin/osh") == "~/.config/oil/oshrc"
    assert get_default_rc("/bin/dash") == "~/.profile"
    assert get_default_rc("/bin/bash") == "~/.bashrc"


def test_sectional_setup_preserves_unedited_configuration(tmp_path: Path):
    config_dir = tmp_path / "vsh"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text(
        """
[shell]
inner_shell = "/bin/bash"
overlay_mode = "kitty"
response_bridge = "signal"

[keybinds]
toggle_listen = "ctrl+]"
toggle_listen_triggers = ["1d"]

[stt]
provider = "vosk"
model = "model"
url = "https://example.test/model.zip"
vad_threshold = 2345
vad_silence_limit = 22

[tts]
provider = "none"

[llm]
provider = "none"

[llm.saved]
type = "cli"
command = "assistant --stdin"
"""
    )
    selection = MagicMock()
    selection.execute.return_value = "none"

    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        patch("InquirerPy.inquirer.select", return_value=selection),
    ):
        interactive_setup(section="llm")

    updated = tomllib.loads(config.read_text())
    assert updated["shell"]["overlay_mode"] == "kitty"
    assert updated["shell"]["response_bridge"] == "signal"
    assert updated["stt"]["vad_threshold"] == 2345
    assert updated["stt"]["vad_silence_limit"] == 22
    assert updated["llm"]["saved"] == {"type": "cli", "command": "assistant --stdin"}
