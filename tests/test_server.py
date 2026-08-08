import http.client
import json
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


def test_execute_tool_is_authenticated_and_disabled():
    from vsh.core.server import serve

    shell = SimpleNamespace(
        shell_name="bash",
        shell_pid=1234,
        shell_state="idle",
        voice_status=lambda: {},
        output_snapshot=lambda: b"",
    )
    server = serve(shell, host="127.0.0.1", port=0, token="test-secret")
    assert server is not None
    connection = http.client.HTTPConnection(*server.server_address)
    body = '{"arg":{"command":"pwd"}}'
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
        assert response.status == 410
        assert response.read() == b'{"error": "live shell execution is disabled"}'
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_read_only_endpoints_expose_no_tools_and_snapshot_output():
    from vsh.core.server import serve

    shell = SimpleNamespace(
        shell_name="fish",
        shell_pid=1234,
        shell_state="idle",
        voice_status=lambda: {},
        output_snapshot=lambda: b"\x1b[32mready\x1b[0m\n",
    )
    server = serve(shell, host="127.0.0.1", port=0, token="test-secret")
    connection = http.client.HTTPConnection(*server.server_address)
    headers = {"Authorization": "Bearer test-secret"}
    try:
        connection.request("GET", "/tools", headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"instance_id": "vsh:fish", "tools": []}

        connection.request("GET", "/io/output", headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"output": "ready\n"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
