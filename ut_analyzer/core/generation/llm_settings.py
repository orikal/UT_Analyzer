"""Settings for the corporate OpenAI-compatible LLM API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Size of the model's context window, in tokens. Every prompt budget is derived
# from it, so a large-context model is actually used to its full extent instead
# of being fed a few thousand characters.
DEFAULT_CONTEXT_TOKENS = 128_000
MIN_CONTEXT_TOKENS = 4_000

# Conservative characters-per-token ratio for source code. Code tokenizes worse
# than prose (punctuation, identifiers, indentation), so this stays low on
# purpose: overestimating it is what makes a prompt overflow the real window.
_CHARS_PER_TOKEN = 3.0
# Share of the *input* budget (window minus reply minus overhead) the prompt may
# use. The remaining slack absorbs tokenizer differences between models.
_PROMPT_SHARE = 0.75
# System prompt, chat scaffolding and per-message overhead.
_RESERVED_TOKENS = 1_200
_MIN_PROMPT_CHARS = 4_000

# How the prompt budget is split between the sections that can grow.
_SHARES = {
    "max_source_chars": 0.21,
    "max_context_chars": 0.23,
    "max_ut_library_chars": 0.24,
    "max_repo_knowledge_chars": 0.15,
    "max_existing_test_chars": 0.11,
    "max_knowledge_docs_chars": 0.06,
}

DEFAULT_MAX_OUTPUT_TOKENS = 8192

# A reasoning model that loops keeps restating the same plan and never writes a
# test. Stop reading once the hidden thinking grew this many times larger than
# the reply we actually asked for.
_REASONING_BUDGET_FACTOR = 4
_MIN_REASONING_CHARS = 20_000


def reasoning_char_limit(context_tokens: int, max_output_tokens: int) -> int:
    """Characters of pure thinking to tolerate before declaring a reasoning loop."""
    reply = output_token_budget(context_tokens, max_output_tokens)
    return max(
        _MIN_REASONING_CHARS, int(reply * _CHARS_PER_TOKEN * _REASONING_BUDGET_FACTOR)
    )


def output_token_budget(context_tokens: int, max_output_tokens: int) -> int:
    """Reply tokens that actually fit next to the prompt in this window.

    A reply budget larger than the window itself is rejected by most gateways,
    so it is clamped to half of the window.
    """
    tokens = max(int(context_tokens), MIN_CONTEXT_TOKENS)
    return max(1, min(int(max_output_tokens), tokens // 2))


def input_token_budget(
    context_tokens: int, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
) -> int:
    """Tokens left for the prompt once the reply and the overhead are reserved."""
    tokens = max(int(context_tokens), MIN_CONTEXT_TOKENS)
    reply = output_token_budget(tokens, max_output_tokens)
    return max(tokens - reply - _RESERVED_TOKENS, tokens // 8)


def prompt_char_budget(
    context_tokens: int, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
) -> int:
    """Characters the whole prompt may use for a model with this context window."""
    usable = input_token_budget(context_tokens, max_output_tokens)
    return max(int(usable * _CHARS_PER_TOKEN * _PROMPT_SHARE), _MIN_PROMPT_CHARS)


def budgets_for_context(
    context_tokens: int, max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
) -> dict[str, int]:
    """Per-section character budgets derived from the model's context window."""
    total = prompt_char_budget(context_tokens, max_output_tokens)
    return {name: int(total * share) for name, share in _SHARES.items()}


def estimate_tokens(chars: int) -> int:
    """Conservative token count for a piece of text of ``chars`` characters."""
    return int(max(chars, 0) / _CHARS_PER_TOKEN) + 1


# Never ask for fewer reply tokens than this: below it the model cannot finish a
# single test case, so a smaller request is pointless.
MIN_OUTPUT_TOKENS = 512


def fit_output_budget(
    context_tokens: int, max_output_tokens: int, prompt_chars: int
) -> int:
    """Reply tokens that still fit next to a prompt of this size.

    Requesting the full reply budget on top of a large prompt is what makes a
    gateway reject the call outright, even though the prompt itself would fit.
    """
    window = max(int(context_tokens), MIN_CONTEXT_TOKENS)
    wanted = output_token_budget(window, max_output_tokens)
    room = window - estimate_tokens(prompt_chars) - _RESERVED_TOKENS
    if room >= wanted:
        return wanted
    return max(MIN_OUTPUT_TOKENS, min(wanted, room))


_DEFAULTS = budgets_for_context(DEFAULT_CONTEXT_TOKENS)

DEFAULT_MAX_SOURCE_CHARS = _DEFAULTS["max_source_chars"]
DEFAULT_MAX_EXISTING_TEST_CHARS = _DEFAULTS["max_existing_test_chars"]
DEFAULT_MAX_CONTEXT_CHARS = _DEFAULTS["max_context_chars"]
DEFAULT_MAX_REPO_KNOWLEDGE_CHARS = _DEFAULTS["max_repo_knowledge_chars"]
DEFAULT_MAX_UT_LIBRARY_CHARS = _DEFAULTS["max_ut_library_chars"]
DEFAULT_MAX_KNOWLEDGE_DOCS_CHARS = _DEFAULTS["max_knowledge_docs_chars"]

# Windows the gateway actually reported, per model name. The configured value is
# only ever a guess; once a provider says "maximum context length is N tokens"
# there is no reason to keep overflowing the same request for every other file.
_LEARNED_CONTEXT_LIMITS: dict[str, int] = {}
# Optional hook so the UI can persist a learned window across restarts.
_LIMIT_OBSERVER: Callable[[str, int], None] | None = None


def set_context_limit_observer(observer: Callable[[str, int], None] | None) -> None:
    """Register a callback invoked whenever a smaller real window is learned."""
    global _LIMIT_OBSERVER
    _LIMIT_OBSERVER = observer


def _model_key(model: str) -> str:
    return (model or "").strip().lower()


def remember_context_limit(model: str, tokens: int) -> int:
    """Record the real window of ``model``; returns the value now in effect."""
    key = _model_key(model)
    tokens = int(tokens)
    if not key or tokens < MIN_CONTEXT_TOKENS:
        return learned_context_limit(model)
    known = _LEARNED_CONTEXT_LIMITS.get(key, 0)
    if not known or tokens < known:
        _LEARNED_CONTEXT_LIMITS[key] = tokens
        if _LIMIT_OBSERVER is not None:
            try:
                _LIMIT_OBSERVER(model, tokens)
            except Exception:  # noqa: BLE001 - persistence must never break a run
                pass
    return _LEARNED_CONTEXT_LIMITS[key]


def learned_context_limit(model: str) -> int:
    """The real window learned for ``model``, or 0 when nothing was learned yet."""
    return _LEARNED_CONTEXT_LIMITS.get(_model_key(model), 0)


def forget_context_limits() -> None:
    """Drop everything learned (used when the model or endpoint changes)."""
    _LEARNED_CONTEXT_LIMITS.clear()


@dataclass
class LLMSettings:
    model: str = ""
    timeout: float = 600.0
    temperature: float = 0.2
    api_base_url: str = ""
    api_key: str = ""
    ssl_verify: bool = True
    context_tokens: int = DEFAULT_CONTEXT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    # Ask the gateway to turn the model's thinking mode off. Reasoning models
    # otherwise spend the whole reply budget deliberating and return no tests.
    disable_thinking: bool = True
    # 0 means "derive it from context_tokens" (see __post_init__).
    max_source_chars: int = 0
    max_existing_test_chars: int = 0
    max_context_chars: int = 0
    max_repo_knowledge_chars: int = 0
    max_ut_library_chars: int = 0
    max_knowledge_docs_chars: int = 0

    def __post_init__(self) -> None:
        for name, value in budgets_for_context(
            self.context_tokens, self.max_output_tokens
        ).items():
            if getattr(self, name) <= 0:
                setattr(self, name, value)

    def effective_context_tokens(self) -> int:
        """The window to plan with: the configured one, capped by what was learned.

        A window configured larger than the model's real one is the single most
        common cause of "the prompt did not fit" loops, so a limit reported by
        the gateway always wins.
        """
        learned = learned_context_limit(self.model)
        if learned and learned < self.context_tokens:
            return learned
        return self.context_tokens

    def effective_budgets(self) -> dict[str, int]:
        """Per-section character budgets for the window really available."""
        tokens = self.effective_context_tokens()
        if tokens >= self.context_tokens:
            return {name: int(getattr(self, name)) for name in _SHARES}
        return budgets_for_context(tokens, self.max_output_tokens)

    def prompt_char_budget(self) -> int:
        """Hard cap for the assembled prompt."""
        return prompt_char_budget(
            self.effective_context_tokens(), self.max_output_tokens
        )

    def output_token_budget(self) -> int:
        """Reply tokens to ask for; never more than the window can hold."""
        return output_token_budget(
            self.effective_context_tokens(), self.max_output_tokens
        )

    def reasoning_char_limit(self) -> int:
        """How much thinking to read before giving up on a looping model."""
        return reasoning_char_limit(
            self.effective_context_tokens(), self.max_output_tokens
        )

    def requires_api_key(self) -> bool:
        return True
