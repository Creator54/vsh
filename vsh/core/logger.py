from typing import Any

_real_logger = None


def get_logger():
    global _real_logger
    if _real_logger is None:
        from loguru import logger as _loguru_logger

        _real_logger = _loguru_logger
    return _real_logger


class LazyLogger:
    """Zero-overhead proxy that defers Loguru import until first log call."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_logger(), name)


logger = LazyLogger()
