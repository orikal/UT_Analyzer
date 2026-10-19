"""Cooperative cancellation for long-running pipeline operations."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

CancelFn = Callable[[], bool]

_cancel_check: ContextVar[CancelFn | None] = ContextVar("cancel_check", default=None)


class OperationCancelled(Exception):
    """Raised when the user cancels a running operation."""


def set_cancellation_check(cancelled: CancelFn | None) -> Token:
    return _cancel_check.set(cancelled)


def reset_cancellation_check(token: Token) -> None:
    _cancel_check.reset(token)


def raise_if_cancelled(cancelled: CancelFn | None = None) -> None:
    """Abort the current operation if cancellation was requested."""
    check = cancelled if cancelled is not None else _cancel_check.get()
    if check is not None and check():
        raise OperationCancelled()
