import http.client
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_execute_tool_requires_bearer_token():
    from vsh.core.server import serve

    shell = SimpleNamespace(
        shell_name="bash",
        shell_pid=1234,
        shell_state="idle",
        voice_status=lambda: {},
        output_history=[],
        exec_command=MagicMock(return_value=("ok", 0)),
    )
    server = serve(shell, host="127.0.0.1", port=0, token="test-secret")
    assert server is not None
    connection = http.client.HTTPConnection(*server.server_address)
    body = json.dumps({"arg": {"command": "pwd"}})
    try:
        connection.request("POST", "/execute_tool", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 401
        response.read()

        connection.request(
            "POST",
            "/execute_tool",
            body=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-secret"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["exit_code"] == 0
        shell.exec_command.assert_called_once_with("pwd")
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
