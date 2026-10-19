"""Shared errors for LLM providers."""

from __future__ import annotations

import re

# Wordings gateways use when the prompt does not fit into the model's window.
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "max seq len",
    "max_seq_len",
    "too many tokens",
    "token limit",
    "prompt is too long",
    "input is too long",
    "request too large",
    "reduce the length",
    "reduce the number of tokens",
    "exceeds the model",
    "exceeds the available context",
)

# The real window, as reported by the gateway. Every wording seen in the wild
# from OpenAI / Azure / vLLM / SGLang / llama.cpp / Anthropic-style proxies.
_LIMIT_PATTERNS = (
    r"maximum context length is\s*(\d+)",
    r"maximum context length of\s*(\d+)",
    r"maximum context length\D{0,12}?(\d+)",
    r"max(?:imum)?[ _]context[ _](?:size|window)\D{0,12}?(\d+)",
    r"max[ _]seq[ _]len\D{0,12}?(\d+)",
    r"context length of only\s*(\d+)",
    r">\s*(\d+)\s*maximum",
    r"limit(?:ed)? to\s*(\d+)\s*tokens",
)
# How many tokens the rejected request asked for in total (prompt + completion).
_REQUESTED_PATTERNS = (
    r"you requested\s*(\d+)",
    r"requested\s*(\d+)\s*tokens",
    r"resulted in\s*(\d+)\s*tokens",
    r"prompt is too long:\s*(\d+)",
    r"(\d+)\s*tokens\s*>",
)
# Completion tokens the rejected request reserved, when the gateway breaks the
# total down ("... (6000 in the messages, 4000 in the completion)").
_COMPLETION_PATTERNS = (
    r"(\d+)\s*in the completion",
    r"(\d+)\s*for the completion",
    r"completion tokens?\D{0,8}?(\d+)",
)
_PROMPT_PATTERNS = (
    r"(\d+)\s*in the messages",
    r"(\d+)\s*in your prompt",
    r"prompt tokens?\D{0,8}?(\d+)",
)

# Anything outside this range is a parsing accident, not a context window.
_MIN_PLAUSIBLE_LIMIT = 512
_MAX_PLAUSIBLE_LIMIT = 10_000_000


def is_context_overflow(status_code: int, body: str) -> bool:
    """True when the gateway rejected the request because the prompt was too big."""
    if status_code == 413:
        return True
    if status_code not in (400, 422, 500):
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def _first_number(text: str, patterns: tuple[str, ...]) -> int:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is None:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if _MIN_PLAUSIBLE_LIMIT <= value <= _MAX_PLAUSIBLE_LIMIT:
            return value
    return 0


def parse_context_limit(body: str) -> "ContextLimits":
    """Read the real window out of a gateway's overflow message.

    Gateways almost always spell out the numbers ("maximum context length is
    8192 tokens. However, you requested 12288 tokens (4096 in the messages,
    8192 in the completion)"). Reading them turns a blind retry loop into a
    single correctly sized request.
    """
    if not body:
        return ContextLimits()
    text = body.replace(",", "")
    limit = _first_number(text, _LIMIT_PATTERNS)
    requested = _first_number(text, _REQUESTED_PATTERNS)
    completion = _first_number(text, _COMPLETION_PATTERNS)
    prompt = _first_number(text, _PROMPT_PATTERNS)
    if not prompt and requested and completion and requested > completion:
        prompt = requested - completion
    if not requested and prompt and completion:
        requested = prompt + completion
    return ContextLimits(
        limit_tokens=limit,
        requested_tokens=requested,
        prompt_tokens=prompt,
        completion_tokens=completion,
    )


class ContextLimits:
    """Token counts extracted from a context-overflow message (0 = unknown)."""

    __slots__ = ("limit_tokens", "requested_tokens", "prompt_tokens", "completion_tokens")

    def __init__(
        self,
        limit_tokens: int = 0,
        requested_tokens: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self.limit_tokens = limit_tokens
        self.requested_tokens = requested_tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def prompt_fits(self, reserve_tokens: int) -> bool:
        """True when only the reply budget pushed the request over the window."""
        if not self.limit_tokens or not self.prompt_tokens:
            return False
        return self.prompt_tokens + max(reserve_tokens, 0) <= self.limit_tokens

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ContextLimits(limit={self.limit_tokens}, requested={self.requested_tokens}, "
            f"prompt={self.prompt_tokens}, completion={self.completion_tokens})"
        )


class LLMError(Exception):
    """Raised when an LLM provider cannot complete a request."""


class ContextLengthError(LLMError):
    """Raised when the request did not fit into the model's context window.

    The caller can recover from this by rebuilding a smaller prompt, so it is a
    distinct type instead of a generic transport failure. ``limits`` carries the
    token counts the gateway reported, so the retry can be sized correctly
    instead of guessing.
    """

    def __init__(self, message: str, *, limits: ContextLimits | None = None) -> None:
        super().__init__(message)
        self.limits = limits or ContextLimits()

    @property
    def limit_tokens(self) -> int:
        return self.limits.limit_tokens


class GenerationError(LLMError):
    """Raised when a request completed but produced no usable test cases.

    Carries a user-facing explanation so the UI can tell the user exactly why
    nothing was generated instead of silently ending the operation.
    """

    def __init__(self, message: str, *, response_preview: str = "") -> None:
        super().__init__(message)
        self.response_preview = response_preview
