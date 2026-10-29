"""Read-only HTTP over a private Unix socket. Live execution stays disabled."""

import errno
import fcntl
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer


def health_payload(shell):
    return {
        "status": "ok",
        "shell": shell.shell_name,
        "pid": shell.shell_pid,
        "state": shell.shell_state,
        "voice": shell.voice_status(),
    }


def make_handler(shell, instance_id: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                self._send(200, health_payload(shell))
            elif self.path.rstrip("/") == "/tools":
                self._send(200, {"instance_id": instance_id, "tools": []})
            elif self.path.rstrip("/") == "/io/output":
                from vsh.core.pty_shell import _clean_output

                self._send(200, {"output": _clean_output(shell.output_snapshot())})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") == "/execute_tool":
                self._send(410, {"error": "live shell execution is disabled"})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def _runtime_directory() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
        info = base.lstat()
        if (
            not base.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise PermissionError("XDG_RUNTIME_DIR must be an owned directory not writable by others")
        directory = base / "vsh"
    else:
        directory = Path(tempfile.gettempdir()) / f"vsh-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


@contextmanager
def _locked_directory(directory):
    # Serialize cooperating instances, including stale detection and shutdown.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("vsh socket directory must be owned by this user with mode 0700")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _identity(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _remove_stale(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise FileExistsError(f"Refusing to replace non-owned socket: {path}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(path)
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise
        else:
            raise FileExistsError(f"Socket is already live: {path}")
    if _identity(path) != (info.st_dev, info.st_ino):
        raise FileExistsError(f"Socket changed during stale check: {path}")
    os.unlink(path)


class VshHTTPServer(ThreadingUnixStreamServer):
    daemon_threads = True

    def server_close(self):
        super().server_close()
        if self.socket_identity is not None:
            with _locked_directory(Path(self.server_address).parent):
                if _identity(self.server_address) == self.socket_identity:
                    os.unlink(self.server_address)
            self.socket_identity = None


def serve(shell, instance_id: str | None = None):
    instance_id = secrets.token_hex(8) if instance_id is None else instance_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", instance_id):
        raise ValueError("instance ID must be 1–32 ASCII letters, digits, underscores or hyphens")
    directory = _runtime_directory()
    path = str(directory / f"{instance_id}.sock")
    server = VshHTTPServer(path, make_handler(shell, instance_id), bind_and_activate=False)
    server.socket_identity = None
    try:
        with _locked_directory(directory):
            _remove_stale(path)
            server.server_bind()
            server.socket_identity = _identity(path)
            os.chmod(path, 0o600)
            server.server_activate()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        return server
    except BaseException:
        server.server_close()
        raise
