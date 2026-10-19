"""Background worker infrastructure for long-running pipeline operations."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from ..core.cancellation import (
    OperationCancelled,
    reset_cancellation_check,
    set_cancellation_check,
)
from ..core.generation.llm_errors import LLMError
from ..core.generation.stream_sink import reset_stream_sink, set_stream_sink
from ..core.knowledge.session_cache import CacheError


class Worker(QThread):
    """Runs ``fn(progress, cancelled)`` off the UI thread.

    ``fn`` receives a progress callback (str) and a cancelled predicate, and
    returns an arbitrary result delivered via ``finished_ok``.
    """

    progress = Signal(str)
    stream = Signal(str, str)  # (channel, text) live model output
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: Callable[..., Any], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102 - QThread override
        token = set_cancellation_check(self.is_cancelled)
        stream_token = set_stream_sink(self.stream.emit)
        try:
            result = self._fn(self.progress.emit, self.is_cancelled)
        except OperationCancelled:
            self.cancelled.emit()
            return
        except (LLMError, CacheError) as exc:
            if self.is_cancelled():
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # deliberately broad: report any pipeline error
            if self.is_cancelled():
                self.cancelled.emit()
                return
            detail = traceback.format_exc(limit=6)
            self.failed.emit(f"{exc}\n\n{detail}")
            return
        finally:
            reset_stream_sink(stream_token)
            reset_cancellation_check(token)

        if self.is_cancelled():
            self.cancelled.emit()
            return
        self.finished_ok.emit(result)
