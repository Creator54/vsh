from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

from vsh import main as main_module
from vsh.core.config import VshConfig
from vsh.core.pty_shell import PtyShell


def _run_main(monkeypatch, *, serve=False, port=8770):
    config = VshConfig()
    config.tts.provider = ""
    shell = MagicMock()

    monkeypatch.delenv("VSH_ACTIVE", raising=False)
    monkeypatch.delenv("VSH_ACTIVE_TTY", raising=False)
    monkeypatch.setattr(main_module, "setup_logger", lambda _verbose: None)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    resolve_tts = MagicMock()
    monkeypatch.setattr(main_module, "resolve_tts", resolve_tts)
    constructor = MagicMock(return_value=shell)
    monkeypatch.setattr(main_module, "PtyShell", constructor)

    main_module.main(
        SimpleNamespace(invoked_subcommand=None),
        voice=False,
        v=False,
        echo=False,
        no_overlay=False,
        serve=serve,
        port=port,
    )
    shell.resolve_tts = resolve_tts
    shell.constructor = constructor
    return shell


def test_serve_is_deferred_to_the_pty_parent(monkeypatch):
    shell = _run_main(monkeypatch, serve=True, port=4567)

    assert shell.constructor.call_args.kwargs["server_port"] == 4567
    shell.run.assert_called_once_with()
    shell.resolve_tts.assert_not_called()


def test_server_is_disabled_by_default(monkeypatch):
    shell = _run_main(monkeypatch)

    assert shell.constructor.call_args.kwargs["server_port"] is None
    shell.run.assert_called_once_with()


def test_default_shell_arguments_include_argv_zero():
    shell = PtyShell(VshConfig())

    assert shell.inner_shell_args == [shell.inner_shell]


def test_unknown_command_returns_usage_error():
    result = CliRunner().invoke(main_module.app, ["definitely-not-a-command"])

    assert result.exit_code == 2


def test_active_vsh_guard_is_scoped_to_the_current_terminal(monkeypatch):
    monkeypatch.setenv("VSH_ACTIVE_TTY", "/dev/pts/old")
    monkeypatch.setattr(main_module.sys, "stdin", SimpleNamespace(fileno=lambda: 0))
    monkeypatch.setattr(main_module.os, "ttyname", lambda _fd: "/dev/pts/new")
    assert not main_module._is_vsh_active_on_this_terminal()

    monkeypatch.setattr(main_module.os, "ttyname", lambda _fd: "/dev/pts/old")
    assert main_module._is_vsh_active_on_this_terminal()
