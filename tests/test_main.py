from types import SimpleNamespace
from unittest.mock import MagicMock

from vsh import main as main_module
from vsh.core.config import VshConfig


def _run_main(monkeypatch, *, serve=False, port=8770):
    config = VshConfig()
    config.tts.provider = ""
    shell = MagicMock()

    monkeypatch.delenv("VSH_ACTIVE", raising=False)
    monkeypatch.setattr(main_module, "setup_logger", lambda _verbose: None)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "resolve_tts", lambda _config: None)
    monkeypatch.setattr(main_module, "PtyShell", lambda *_args, **_kwargs: shell)

    main_module.main(
        SimpleNamespace(invoked_subcommand=None),
        voice=False,
        v=False,
        echo=False,
        no_overlay=False,
        serve=serve,
        port=port,
    )
    return shell


def test_serve_starts_the_http_bridge(monkeypatch):
    from vsh.core import server

    serve = MagicMock()
    monkeypatch.setattr(server, "serve", serve)

    shell = _run_main(monkeypatch, serve=True, port=4567)

    serve.assert_called_once_with(shell, port=4567)
    shell.run.assert_called_once_with()


def test_bridge_failure_does_not_stop_the_shell(monkeypatch):
    from vsh.core import server

    monkeypatch.setattr(server, "serve", MagicMock(side_effect=OSError("port busy")))

    shell = _run_main(monkeypatch, serve=True, port=4567)

    shell.run.assert_called_once_with()
