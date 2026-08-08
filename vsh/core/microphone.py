import fcntl
import os
from pathlib import Path
from typing import TextIO


def acquire_microphone() -> TextIO | None:
    """Acquire the per-user microphone lease without blocking."""
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".cache")) / "vsh"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (runtime_dir / "microphone.lock").open("a+")
    os.chmod(lock.name, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def release_microphone(lock: TextIO | None) -> None:
    if lock is not None:
        lock.close()
