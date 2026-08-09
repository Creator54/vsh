"""Expose a live shell on a local-only web server.

Endpoints: /health, /tools, /io/output. Live command execution is disabled.
"""

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loguru import logger


def health_payload(shell):
    return {
        "status": "ok",
        "shell": shell.shell_name,
        "pid": shell.shell_pid,
        "state": shell.shell_state,
        "voice": shell.voice_status(),
    }


def _tools_schema():
    return []


def make_handler(shell, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence stdlib access log
            pass

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return secrets.compare_digest(supplied, expected)

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            if self.path.rstrip("/") == "/health":
                self._send(200, health_payload(shell))
            elif self.path.rstrip("/") == "/tools":
                self._send(200, {"instance_id": f"vsh:{shell.shell_name}", "tools": _tools_schema()})
            elif self.path.rstrip("/") == "/io/output":
                from vsh.core.pty_shell import _clean_output

                raw = shell.output_snapshot()
                self._send(200, {"output": _clean_output(raw)})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") != "/execute_tool":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            self._send(410, {"error": "live shell execution is disabled"})

    return Handler


class VshHTTPServer(ThreadingHTTPServer):
    auth_token: str


def serve(shell, host="127.0.0.1", port=8770, max_attempts=10, token: str | None = None):
    token = token or os.environ.get("VSH_SERVER_TOKEN") or secrets.token_urlsafe(32)
    for current_port in range(port, port + max_attempts):
        try:
            srv = VshHTTPServer((host, current_port), make_handler(shell, token))
            srv.auth_token = token
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            logger.debug(f"VSH HTTP server bound to {host}:{current_port}")
            return srv
        except OSError as e:
            logger.debug(f"Port {current_port} busy ({e}), trying next...")
            continue
    logger.warning("Could not bind VSH HTTP server: all ports in range busy.")
    return None
