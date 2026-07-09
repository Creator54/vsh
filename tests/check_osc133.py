"""Self-check for OSC 133 parsing + ANSI stripping in pty_shell.

Run: python sidecars/vsh/tests/check_osc133.py
ponytail: no framework — asserts the parser logic the exec path depends on.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ponytail: stub pyaudio — config imports it, the parser doesn't need it.
sys.modules.setdefault("pyaudio", types.ModuleType("pyaudio"))

from vsh.core.pty_shell import _OSC133, _strip_ansi  # noqa: E402


def _parse(data: bytes):
    """Mirror _scan_osc133's capture logic without a PtyShell instance."""
    buf = None
    exit_code = None
    for m in _OSC133.finditer(data):
        if m.group(1) == b"C":
            buf = bytearray()
        elif m.group(1) == b"D" and buf is not None:
            exit_code = int(m.group(2) or 0)
    # capture appends whole stream once started; emulate by taking post-C slice
    ci = data.find(b"\x1b]133;C")
    di = data.find(b"\x1b]133;D")
    body = data[ci:di] if ci != -1 and di != -1 else b""
    return _strip_ansi(body), exit_code


# BEL-terminated markers, exit 0
out, code = _parse(b"\x1b]133;C\x07hello\r\n\x1b]133;D;0\x07")
assert code == 0, code
assert b"hello" in out, out

# ST-terminated markers, nonzero exit
out, code = _parse(b"\x1b]133;C\x1b\\oops\n\x1b]133;D;1\x1b\\")
assert code == 1, code
assert b"oops" in out, out

# D without exit code defaults to 0
out, code = _parse(b"\x1b]133;C\x07x\x1b]133;D\x07")
assert code == 0, code

# ANSI colour codes stripped from captured body
out, _ = _parse(b"\x1b]133;C\x07\x1b[31mred\x1b[0m\x1b]133;D;0\x07")
assert out.strip() == b"red", out

print("OK: OSC133 parse + strip self-check passed")
