"""Expose a live shell on a local-only web server.

Endpoints: /health, /tools, /io/output, /execute_tool.
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


def _tools_schema(shell):
    return [
        {
            "name": "vsh_run_command",
            "description": (
                f"Run a command in the user's live {shell.shell_name} session and return its output and exit code. "
                "Fails if the shell is busy."
            ),
            "keywords": ["vsh", "shell", "run", "command", "live"],
            "params": {"command": {"type": "str", "required": True}},
        }
    ]


_MAX_REQUEST_BYTES = 1024 * 1024


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
                self._send(200, {"instance_id": f"vsh:{shell.shell_name}", "tools": _tools_schema(shell)})
            elif self.path.rstrip("/") == "/io/output":
                from vsh.core.pty_shell import _clean_output

                raw = b"".join(shell.output_history)
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
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "content type must be application/json"})
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n < 0 or n > _MAX_REQUEST_BYTES:
                    self._send(413, {"error": "request body too large"})
                    return
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict) or not isinstance(req.get("arg"), dict):
                    self._send(400, {"error": "body must contain an arg object"})
                    return
                cmd = req["arg"].get("command", "")
                if not isinstance(cmd, str) or not cmd.strip():
                    self._send(400, {"error": "command must be a non-empty string"})
                    return
                out, code = shell.exec_command(cmd)
                self._send(200, {"status": "ok", "command": cmd, "output": out, "exit_code": code})
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._send(400, {"status": "error", "output": str(error)})
            except RuntimeError as error:
                self._send(409, {"status": "error", "output": str(error)})
            except TimeoutError as error:
                self._send(504, {"status": "error", "output": str(error)})

    return Handler


def serve(shell, host="127.0.0.1", port=8770, max_attempts=10, token: str | None = None):
    token = token or os.environ.get("VSH_SERVER_TOKEN") or secrets.token_urlsafe(32)
    for current_port in range(port, port + max_attempts):
        try:
            srv = ThreadingHTTPServer((host, current_port), make_handler(shell, token))
            srv.auth_token = token
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            logger.debug(f"VSH HTTP tool server bound to {host}:{current_port}")
            return srv
        except OSError as e:
            logger.debug(f"Port {current_port} busy ({e}), trying next...")
            continue
    logger.warning("Could not bind VSH HTTP tool server: all ports in range busy.")
    return None
