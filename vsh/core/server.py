"""Expose a live shell on a local-only web server.

Endpoints: /health, /tools, /io/output, /execute_tool.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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


def make_handler(shell):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence stdlib access log
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
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                cmd = (req.get("arg") or {}).get("command", "")
                out, code = shell.exec_command(cmd)
                self._send(200, {"status": "ok", "command": cmd, "output": out, "exit_code": code})
            except Exception as e:
                self._send(200, {"status": "error", "output": str(e)})

    return Handler


def serve(shell, host="127.0.0.1", port=8770):
    srv = ThreadingHTTPServer((host, port), make_handler(shell))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
