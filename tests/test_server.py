import http.client
import json
import os
import socket
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from types import SimpleNamespace

import pytest

from vsh.core.server import health_payload, serve


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=2)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def shell(output=b"ready\n"):
    return SimpleNamespace(
        shell_name="fish", shell_pid=4321, shell_state="idle", voice_status=lambda: {}, output_snapshot=lambda: output
    )


def request(server, path, method="GET"):
    with closing(UnixConnection(server.server_address)) as connection:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())


def close(server):
    server.shutdown()
    server.server_close()


def test_health_exposes_voice_state():
    instance = shell()
    voice = {"enabled": True, "mic_muted": True, "phase": "thinking", "visual_state": "muted"}
    instance.voice_status = lambda: voice
    assert health_payload(instance) == {"status": "ok", "shell": "fish", "pid": 4321, "state": "idle", "voice": voice}


def test_read_only_endpoints_and_permissions():
    server = serve(shell(b"\x1b[32mready\x1b[0m\n"), instance_id="first")
    try:
        assert stat.S_IMODE(os.stat(server.server_address).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(server.server_address)).st_mode) == 0o700
        assert request(server, "/health")[0] == 200
        assert request(server, "/tools") == (200, {"instance_id": "first", "tools": []})
        assert request(server, "/io/output") == (200, {"output": "ready\n"})
        assert request(server, "/execute_tool", "POST")[0] == 410
        assert request(server, "/missing")[0] == 404
    finally:
        close(server)
    assert not os.path.exists(server.server_address)


def test_instances_reconnect_and_restart_without_crossing_output():
    first = serve(shell(b"one"), instance_id="one")
    second = serve(shell(b"two"), instance_id="two")
    try:
        for _ in range(2):
            assert request(first, "/io/output") == (200, {"output": "one"})
            assert request(second, "/io/output") == (200, {"output": "two"})
        with pytest.raises(OSError):
            serve(shell(), instance_id="one")
        assert request(first, "/health")[0] == 200
    finally:
        close(first)
        close(second)
    restarted = serve(shell(b"new"), instance_id="one")
    try:
        assert request(restarted, "/io/output") == (200, {"output": "new"})
    finally:
        close(restarted)


def test_concurrent_startup_accepts_only_one_instance():
    barrier = Barrier(2)

    def start():
        barrier.wait(timeout=2)
        try:
            return serve(shell(), instance_id="race")
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        servers = list(pool.map(lambda _: start(), range(2)))
    winners = [server for server in servers if server is not None]
    try:
        assert len(winners) == 1
        assert request(winners[0], "/health")[0] == 200
    finally:
        for server in winners:
            close(server)


def test_stale_socket_is_reclaimed_but_live_socket_is_not(tmp_path):
    directory = tmp_path / "vsh"
    directory.mkdir(mode=0o700)
    path = directory / "stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = serve(shell(), instance_id="stale")
    try:
        assert request(server, "/health")[0] == 200
    finally:
        close(server)
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(path))
    live.listen()
    try:
        with pytest.raises(OSError):
            serve(shell(), instance_id="stale")
        assert path.exists()
    finally:
        live.close()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_unrelated_files_are_never_overwritten(tmp_path, kind):
    directory = tmp_path / "vsh"
    directory.mkdir(mode=0o700)
    path = directory / "other.sock"
    target = tmp_path / "target"
    target.write_text("keep")
    if kind == "file":
        path.write_text("keep")
    else:
        path.symlink_to(target)
    with pytest.raises(OSError):
        serve(shell(), instance_id="other")
    assert path.read_text() == "keep"


def test_shutdown_preserves_replacement_file():
    server = serve(shell(), instance_id="replace")
    os.unlink(server.server_address)
    with open(server.server_address, "w") as replacement:
        replacement.write("keep")
    close(server)
    with open(server.server_address) as replacement:
        assert replacement.read() == "keep"


@pytest.mark.parametrize("instance_id", ["../escape", "", "a/b", "a" * 100])
def test_invalid_instance_names_are_rejected(instance_id):
    with pytest.raises(ValueError):
        serve(shell(), instance_id=instance_id)


def test_unsafe_runtime_subdirectory_is_rejected(tmp_path):
    directory = tmp_path / "vsh"
    directory.mkdir(mode=0o755)
    with pytest.raises(OSError):
        serve(shell())


def test_bind_failure_cleans_up_only_its_socket(monkeypatch):
    from vsh.core.server import VshHTTPServer

    def fail_activate(self):
        raise OSError("activation failed")

    monkeypatch.setattr(VshHTTPServer, "server_activate", fail_activate)
    with pytest.raises(OSError, match="activation failed"):
        serve(shell(), instance_id="failed")
    runtime = os.environ["XDG_RUNTIME_DIR"]
    assert not os.path.lexists(os.path.join(runtime, "vsh", "failed.sock"))


def test_symlinked_runtime_subdirectory_is_rejected(tmp_path):
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    (tmp_path / "vsh").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        serve(shell())
    assert list(target.iterdir()) == []


def test_concurrent_output_reads_stay_isolated():
    first = serve(shell(b"one"))
    second = serve(shell(b"two"))
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda index: request(first if index % 2 == 0 else second, "/io/output"), range(20))
            )
        assert results == [(200, {"output": "one" if index % 2 == 0 else "two"}) for index in range(20)]
    finally:
        close(first)
        close(second)


def test_fallback_is_private(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = serve(shell())
    try:
        assert os.path.dirname(server.server_address) == str(tmp_path / f"vsh-{os.getuid()}")
        assert stat.S_IMODE(os.stat(os.path.dirname(server.server_address)).st_mode) == 0o700
    finally:
        close(server)
