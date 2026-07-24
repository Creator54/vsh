import tomllib
from pathlib import Path
from unittest.mock import patch

from vsh.core.setup import update_keybind_config, update_shell_rc_bind


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
    assert "if not set -q VSH_ACTIVE; and isatty 1" in content
