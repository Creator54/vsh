from types import SimpleNamespace

from vsh.core.server import health_payload


def test_health_exposes_the_voice_state_used_by_the_hud():
    voice = {
        "enabled": True,
        "mic_muted": True,
        "phase": "thinking",
        "visual_state": "muted",
    }
    shell = SimpleNamespace(
        shell_name="fish",
        shell_pid=4321,
        shell_state="idle",
        voice_status=lambda: voice,
    )

    assert health_payload(shell) == {
        "status": "ok",
        "shell": "fish",
        "pid": 4321,
        "state": "idle",
        "voice": voice,
    }


def test_server_port_fallback_when_initial_port_busy():
    from vsh.core.server import serve

    shell = SimpleNamespace(
        shell_name="bash",
        shell_pid=1234,
        shell_state="idle",
        voice_status=lambda: {},
    )

    srv1 = serve(shell, host="127.0.0.1", port=9870)
    assert srv1 is not None
    try:
        # Second server on same port range should bind to 9871
        srv2 = serve(shell, host="127.0.0.1", port=9870)
        assert srv2 is not None
        assert srv2.server_address[1] == 9871
        srv2.shutdown()
        srv2.server_close()
    finally:
        srv1.shutdown()
        srv1.server_close()
