import fcntl
import multiprocessing
import os
import threading
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import vsh.core.setup as setup_mod
from vsh.core.setup import (
    _locked_file,
    _setup_lock,
    _write_setup_transaction,
    get_default_rc,
    interactive_setup,
    update_keybind_config,
    update_shell_rc_bind,
)


def _update_keybind_in_process(config: str, result) -> None:
    lock_path = Path(config).with_name(f"{Path(config).name}.vsh.lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.put("waiting")
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            result.put("not-waiting")
    try:
        update_keybind_config(Path(config), {"name": "ctrl+g", "triggers": ["07"]})
        result.put(None)
    except Exception as error:
        result.put(repr(error))


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


def test_keybind_update_preserves_a_symlinked_config(tmp_path: Path):
    target = tmp_path / "managed.toml"
    target.write_text('[shell]\ninner_shell = "/bin/fish"\n')
    config = tmp_path / "config.toml"
    config.symlink_to(target)

    update_keybind_config(config, {"name": "ctrl+g", "triggers": ["07"]})

    assert config.is_symlink()
    assert tomllib.loads(target.read_text())["keybinds"]["toggle_listen"] == "ctrl+g"


def test_keybind_update_waits_for_concurrent_config_writer(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[shell]\ninner_shell = "/bin/bash"\n')
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(target=_update_keybind_in_process, args=(str(config), result))

    with _locked_file(config):
        process.start()
        assert result.get(timeout=5) == "waiting"
        config.write_text('[shell]\ninner_shell = "/bin/bash"\n\n[stt]\nprovider = "vosk"\n')

    process.join(timeout=5)
    assert process.exitcode == 0
    assert result.get(timeout=1) is None
    data = tomllib.loads(config.read_text())
    assert data["stt"] == {"provider": "vosk"}
    assert data["keybinds"] == {"toggle_listen": "ctrl+g", "toggle_listen_triggers": ["07"]}


def test_parallel_first_run_setup_prompts_once(tmp_path: Path):
    calls = []
    barrier = threading.Barrier(2)

    def fake_setup(_section, config_path):
        calls.append(config_path)
        config_path.write_text('[tts]\nprovider = "none"\n')

    def run_setup():
        barrier.wait()
        interactive_setup(skip_if_configured=True)

    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}),
        patch.object(setup_mod, "_interactive_setup", side_effect=fake_setup),
    ):
        threads = [threading.Thread(target=run_setup) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

    assert len(calls) == 1
    assert all(not thread.is_alive() for thread in threads)


def test_setup_lock_keeps_config_and_shell_binding_as_one_transaction(tmp_path: Path):
    config = tmp_path / "config.toml"
    shell_rc = tmp_path / "config.fish"
    first_started = threading.Event()
    release_first = threading.Event()
    second_acquired = threading.Event()

    def write_pair(value, pause=False):
        with _setup_lock(config):
            if value == "second":
                second_acquired.set()
            config.write_text(value)
            if pause:
                first_started.set()
                assert release_first.wait(timeout=1)
            shell_rc.write_text(value)

    first = threading.Thread(target=write_pair, args=("first", True))
    second = threading.Thread(target=write_pair, args=("second",))
    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    assert not second_acquired.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert config.read_text() == shell_rc.read_text() == "second"


def test_setup_transaction_validates_shell_binding_before_writing_config(tmp_path: Path):
    config = tmp_path / "config.toml"
    shell_rc = tmp_path / "config.fish"
    config.write_text("old config")
    shell_rc.write_text("old rc")
    unsupported = {"name": "custom", "fish": None}

    try:
        _write_setup_transaction(config, "new config", str(shell_rc), unsupported, False)
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported binding was accepted")

    assert config.read_text() == "old config"
    assert shell_rc.read_text() == "old rc"


def test_setup_transaction_rolls_back_when_second_replace_fails(tmp_path: Path):
    config = tmp_path / "config.toml"
    shell_rc = tmp_path / "config.fish"
    config.write_text("old config")
    shell_rc.write_text("old rc")
    keybind = {"name": "ctrl+]", "fish": "\\c]"}
    real_replace = os.replace
    failed = False

    def fail_rc_once(source, target):
        nonlocal failed
        if Path(target) == shell_rc and not failed:
            failed = True
            raise OSError("injected failure")
        return real_replace(source, target)

    with patch("vsh.core.setup.os.replace", side_effect=fail_rc_once):
        try:
            _write_setup_transaction(config, "new config", str(shell_rc), keybind, False)
        except OSError:
            pass
        else:
            raise AssertionError("replace failure was ignored")

    assert config.read_text() == "old config"
    assert shell_rc.read_text() == "old rc"


def test_setup_transaction_rolls_back_when_interrupted_during_replace(tmp_path: Path):
    config = tmp_path / "config.toml"
    shell_rc = tmp_path / "config.fish"
    config.write_text("old config")
    shell_rc.write_text("old rc")
    keybind = {"name": "ctrl+]", "fish": "\\c]"}
    real_replace = os.replace

    def interrupt_rc(source, target):
        if Path(target) == shell_rc:
            raise KeyboardInterrupt
        return real_replace(source, target)

    with patch("vsh.core.setup.os.replace", side_effect=interrupt_rc):
        try:
            _write_setup_transaction(config, "new config", str(shell_rc), keybind, False)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("interrupt was ignored")

    assert config.read_text() == "old config"
    assert shell_rc.read_text() == "old rc"


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
