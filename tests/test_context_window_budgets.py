"""The context-window setting must stay usable: budgets fit, oversized prompts retry."""

from __future__ import annotations

from pathlib import Path

import pytest

from ut_analyzer.adapters.python.adapter import PythonAdapter
from ut_analyzer.core.generation.llm_errors import (
    ContextLengthError,
    GenerationError,
    is_context_overflow,
    parse_context_limit,
)
from ut_analyzer.core.generation.llm_settings import (
    LLMSettings,
    budgets_for_context,
    fit_output_budget,
    forget_context_limits,
    input_token_budget,
    learned_context_limit,
    output_token_budget,
    prompt_char_budget,
    remember_context_limit,
)
from ut_analyzer.models import EnvironmentInfo, QualityGap, TestConventions

RESPONSE = (
    "=== TEST: test_add | no | adds two numbers | 1, 2 | 3\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n"
    "=== END TEST ==="
)


@pytest.fixture(autouse=True)
def _clean_learned_limits() -> object:
    """The learned-window registry is process-wide; keep tests independent."""
    forget_context_limits()
    yield
    forget_context_limits()


# ---- budget math -----------------------------------------------------------


def test_prompt_budget_reserves_the_reply_and_fits_the_window() -> None:
    for window in (4_000, 8_000, 32_000, 128_000, 1_000_000):
        settings = LLMSettings(context_tokens=window, max_output_tokens=8192)
        prompt_tokens = settings.prompt_char_budget() / 3.0
        assert prompt_tokens + settings.output_token_budget() < window


def test_reply_budget_never_exceeds_half_the_window() -> None:
    assert output_token_budget(8_000, 65_536) == 4_000
    assert output_token_budget(128_000, 8_192) == 8_192


def test_bigger_window_gives_a_bigger_prompt_budget() -> None:
    small = prompt_char_budget(32_000)
    large = prompt_char_budget(128_000)
    assert large > small
    assert input_token_budget(128_000) > input_token_budget(32_000)


def test_section_budgets_sum_to_the_prompt_budget() -> None:
    budgets = budgets_for_context(128_000)
    assert sum(budgets.values()) <= prompt_char_budget(128_000)
    assert all(value > 0 for value in budgets.values())


def test_settings_derive_section_budgets_from_the_window() -> None:
    small = LLMSettings(context_tokens=16_000)
    large = LLMSettings(context_tokens=200_000)
    assert large.max_source_chars > small.max_source_chars
    assert large.max_ut_library_chars > small.max_ut_library_chars


# ---- overflow detection ----------------------------------------------------


def test_context_overflow_is_recognized_from_the_provider_body() -> None:
    body = (
        "{'error': {'message': \"This model's maximum context length is 8192 tokens, "
        'however you requested 40000 tokens."}}'
    )
    assert is_context_overflow(400, body) is True
    assert is_context_overflow(413, "") is True
    assert is_context_overflow(400, "invalid api key") is False
    assert is_context_overflow(401, "context length") is False


def test_overflow_body_reveals_the_real_window() -> None:
    limits = parse_context_limit(
        "This model's maximum context length is 8192 tokens. However, you requested "
        "12288 tokens (4096 in the messages, 8192 in the completion). Please reduce "
        "the length of the messages or completion."
    )
    assert limits.limit_tokens == 8192
    assert limits.requested_tokens == 12288
    assert limits.prompt_tokens == 4096
    assert limits.completion_tokens == 8192
    # Only the reply budget was too big - the prompt itself fits.
    assert limits.prompt_fits(reserve_tokens=1024) is True


def test_overflow_body_variants_are_understood() -> None:
    vllm = parse_context_limit(
        "This model's maximum context length is 32768 tokens. "
        "However, you requested 40000 tokens"
    )
    assert vllm.limit_tokens == 32768
    assert vllm.requested_tokens == 40000

    anthropic = parse_context_limit("prompt is too long: 210000 tokens > 200000 maximum")
    assert anthropic.limit_tokens == 200_000
    assert anthropic.requested_tokens == 210_000

    assert parse_context_limit("").limit_tokens == 0
    assert parse_context_limit("service unavailable").limit_tokens == 0


def test_learned_window_overrides_a_too_large_setting() -> None:
    settings = LLMSettings(model="qwen", context_tokens=128_000)
    full_budget = settings.prompt_char_budget()

    remember_context_limit("qwen", 8_192)

    assert learned_context_limit("QWEN") == 8_192
    assert settings.effective_context_tokens() == 8_192
    assert settings.prompt_char_budget() < full_budget
    assert settings.effective_budgets()["max_source_chars"] < settings.max_source_chars
    # A window the user configured smaller than reality is left alone.
    assert LLMSettings(model="qwen", context_tokens=4_000).effective_context_tokens() == 4_000


def test_reply_budget_shrinks_to_fit_next_to_a_big_prompt() -> None:
    # 90k characters of prompt in an 8k window: the reply must give way.
    small = fit_output_budget(8_000, 8_192, 90_000)
    assert small < output_token_budget(8_000, 8_192)
    assert small >= 512
    # Plenty of room: the full reply budget is kept.
    assert fit_output_budget(128_000, 8_192, 20_000) == 8_192


# ---- shrink and retry ------------------------------------------------------


class _OverflowThenAnswerClient:
    """Rejects the first prompt as too long, then answers the smaller one."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.prompts: list[str] = []

    def generate(self, prompt: str, system: str = "", **_: object) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise ContextLengthError("maximum context length is 8192 tokens")
        return RESPONSE


def _gap(tmp_path: Path) -> QualityGap:
    source = tmp_path / "calc.py"
    source.write_text(
        "def add(a, b):\n    return a + b\n" + "# filler\n" * 4000, encoding="utf-8"
    )
    return QualityGap(
        source_file=source,
        reason="no test file covers this",
        untested_functions=["add"],
    )


def _generate(tmp_path: Path, client: object) -> object:
    # test_generator pulls in the HTTP client stack.
    pytest.importorskip("requests")
    from ut_analyzer.core.generation.test_generator import generate_proposal_for_gap

    return generate_proposal_for_gap(
        gap=_gap(tmp_path),
        adapter=PythonAdapter(),
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        test_files=[],
        client=client,  # type: ignore[arg-type]
        test_dir=tmp_path / "tests",
        root=tmp_path,
        llm_settings=LLMSettings(model="m", context_tokens=128_000),
    )


def test_oversized_prompt_is_retried_smaller(tmp_path: Path) -> None:
    client = _OverflowThenAnswerClient(failures=1)

    change = _generate(tmp_path, client)

    assert change is not None
    assert [test.name for test in change.tests] == ["test_add"]
    assert len(client.prompts) == 2
    assert len(client.prompts[1]) < len(client.prompts[0])


def test_persistent_overflow_explains_the_context_window_setting(tmp_path: Path) -> None:
    client = _OverflowThenAnswerClient(failures=99)

    try:
        _generate(tmp_path, client)
    except GenerationError as exc:
        message = str(exc)
    else:  # pragma: no cover - the client always overflows
        raise AssertionError("expected GenerationError")

    assert len(client.prompts) == 3
    assert "Model context window" in message
    assert "did not fit into the model's context window" in message


# ---- learning the real window ----------------------------------------------


class _ReportsItsWindowClient:
    """Rejects the first prompt, telling the caller how big its window really is."""

    def __init__(self, limit_tokens: int, failures: int = 1) -> None:
        self.limit_tokens = limit_tokens
        self.failures = failures
        self.prompts: list[str] = []

    def generate(self, prompt: str, system: str = "", **_: object) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise ContextLengthError(
                "did not fit",
                limits=parse_context_limit(
                    f"This model's maximum context length is {self.limit_tokens} tokens"
                ),
            )
        return RESPONSE


def test_reported_window_rebuilds_the_prompt_to_fit(tmp_path: Path) -> None:
    client = _ReportsItsWindowClient(limit_tokens=8_192)

    change = _generate(tmp_path, client)

    assert change is not None
    assert len(client.prompts) == 2
    # The retry is sized for the window the gateway named, not a blind fraction.
    assert len(client.prompts[1]) <= prompt_char_budget(8_192, 8_192)
    assert learned_context_limit("m") == 8_192


def test_the_window_is_learned_once_for_the_whole_run(tmp_path: Path) -> None:
    first = _ReportsItsWindowClient(limit_tokens=8_192)
    _generate(tmp_path, first)

    second = _ReportsItsWindowClient(limit_tokens=8_192, failures=0)
    other = tmp_path / "next"
    other.mkdir()
    change = _generate(other, second)

    assert change is not None
    # No wasted round trip: the very first prompt already fits.
    assert len(second.prompts) == 1
    assert len(second.prompts[0]) <= prompt_char_budget(8_192, 8_192)


def test_a_huge_window_does_not_produce_a_huge_prompt(tmp_path: Path) -> None:
    pytest.importorskip("requests")
    from ut_analyzer.core.generation.test_generator import (
        _FOCUSED_PROMPT_CHARS,
        generate_proposal_for_gap,
    )

    client = _OverflowThenAnswerClient(failures=0)
    generate_proposal_for_gap(
        gap=_gap(tmp_path),
        adapter=PythonAdapter(),
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        test_files=[],
        client=client,  # type: ignore[arg-type]
        test_dir=tmp_path / "tests",
        root=tmp_path,
        llm_settings=LLMSettings(model="huge", context_tokens=1_000_000),
    )

    assert len(client.prompts) == 1
    assert len(client.prompts[0]) <= _FOCUSED_PROMPT_CHARS
