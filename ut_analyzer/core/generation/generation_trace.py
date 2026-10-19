"""Append-only trace log for test-generation debugging."""

from __future__ import annotations

import threading
from datetime import datetime

from ...utils.paths import logs_dir

_lock = threading.Lock()
_TRACE_FILE = logs_dir() / "generation_trace.log"


def trace(message: str) -> None:
    """Write a timestamped generation trace line (safe from any thread)."""
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    with _lock:
        _TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _TRACE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line)


def trace_path() -> str:
    return str(_TRACE_FILE)
