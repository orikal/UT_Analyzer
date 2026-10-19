"""Live channel for incremental model output (thinking + generated text).

The LLM client publishes streamed chunks here; the UI worker installs a sink so the
Suggested Tests tab can show the model's work while the tests are still being written.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

CHANNEL_REASONING = "reasoning"
CHANNEL_CONTENT = "content"

StreamFn = Callable[[str, str], None]

_stream_sink: ContextVar[StreamFn | None] = ContextVar("llm_stream_sink", default=None)


def set_stream_sink(sink: StreamFn | None) -> Token:
    return _stream_sink.set(sink)


def reset_stream_sink(token: Token) -> None:
    _stream_sink.reset(token)


def stream_sink() -> StreamFn | None:
    """Return the sink installed for the current operation, if any."""
    return _stream_sink.get()
