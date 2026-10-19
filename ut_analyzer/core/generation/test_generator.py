"""Test generator: turns quality gaps into ProposedFileChange objects via the LLM API."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...adapters.base import LanguageAdapter
from ...core.discovery.test_file_resolver import resolve_target_test_file
from ...models import (
    EnvironmentInfo,
    ProposedFileChange,
    ProposedTest,
    QualityGap,
    RepoKnowledge,
    ScanResult,
    TestConventions,
    TestFile,
    UTKnowledge,
)
from ...utils.fs import read_text_safe
from ..cancellation import raise_if_cancelled
from ..conventions.test_template import derive_test_template
from ..knowledge.knowledge_docs import KnowledgeDocs
from .generation_trace import trace
from .llm_client import LLMClient
from .llm_errors import ContextLengthError, GenerationError
from .llm_settings import (
    DEFAULT_MAX_EXISTING_TEST_CHARS,
    DEFAULT_MAX_KNOWLEDGE_DOCS_CHARS,
    DEFAULT_MAX_REPO_KNOWLEDGE_CHARS,
    DEFAULT_MAX_SOURCE_CHARS,
    DEFAULT_MAX_UT_LIBRARY_CHARS,
    LLMSettings,
    budgets_for_context,
    estimate_tokens,
    learned_context_limit,
    prompt_char_budget,
    remember_context_limit,
)
from .prompt_builder import (
    SYSTEM_PROMPT,
    TEST_END,
    TEST_START,
    build_file_map,
    build_generation_prompt,
    parse_generation_response,
    parse_ut_fixture_response,
    parse_ut_header_response,
)
from .symbol_context import (
    DEFAULT_MAX_CONTEXT_CHARS,
    ConstructorInfo,
    SymbolContext,
    collect_symbol_context,
    condense_declarations,
    constructors_prompt_text,
    ensure_symbol_includes,
    extract_constructors,
    flag_unknown_constructions,
    include_directive_for,
)

# A prompt that overflows the model's window (or simply overwhelms it) is the most
# common reason a generation returns no tests. A retry rebuilds the prompt smaller;
# whole context sections are dropped by prompt_builder._fit_to_budget, the
# instructions the model must follow are always kept.
# Hard cap on how many requests one generation may send, whatever goes wrong.
_MAX_GENERATION_ATTEMPTS = 5
# Applied to the prompt budget when the gateway rejected the request but did not
# say how big its window is: keep shrinking until it fits.
_BLIND_SHRINK = 0.35
# Never shrink the prompt below this: at some point the request is not the problem.
_MIN_PROMPT_SCALE = 0.12
# Extra attempts allowed when the model answered but wrote no test block.
_UNPARSABLE_RETRY_LIMIT = 2
# From this unparsable retry on, the prompt is stripped down to source code +
# output format: no UT header/fixture block to write first, no repository
# context. A model that drowned in context usually writes tests from this.
_MINIMAL_PROMPT_AFTER_RETRIES = 2
# Appended to the prompt after a reply that carried no test code at all: small
# models follow a short, blunt restatement of the output format much better than
# the full instruction block.
_RETRY_NUDGE = (
    "IMPORTANT - your previous answer contained no test code and was rejected. "
    "Answer with test code only, no explanation, no plan, no apology. "
    f"For EACH test write one line `{TEST_START} <test_name> | no | <description> "
    "| <input params> | <expected output>`, then the complete test function, then "
    f"`{TEST_END}` on its own line. Start your answer with the first "
    f"`{TEST_START}` line."
)
_MIN_SECTION_CHARS = 400
# Asking a struggling model for fewer tests makes a complete, parsable reply much
# more likely than asking for the same thing again.
_MIN_TESTS_ON_RETRY = 2
# Practical ceiling for one generation prompt, independent of how large the window
# is. Everything above this is slower, more expensive and *less* accurate: the
# relevant source, API declarations and UT examples already fit here, and a model
# that has to read 250k characters loses track of them. Raising the configured
# window no longer means every request has to be gigantic.
#
# It is also what keeps a request inside a window that is *smaller* than the one
# configured in Settings: 24k characters is roughly 10k tokens, so the very first
# prompt already fits the small windows most corporate gateways actually serve.
_FOCUSED_PROMPT_CHARS = 24_000


@dataclass(frozen=True)
class _PromptBudgets:
    """Character budgets for one generation, scalable for shrink-and-retry."""

    total: int
    source: int
    existing_test: int
    ut_library: int
    api_context: int
    repo_knowledge: int
    knowledge_docs: int

    @classmethod
    def _focused(cls, total: int, sections: dict[str, int]) -> "_PromptBudgets":
        """Budgets clamped to the focused prompt size, keeping section ratios."""
        ratio = 1.0
        if total > _FOCUSED_PROMPT_CHARS:
            ratio = _FOCUSED_PROMPT_CHARS / total
            total = _FOCUSED_PROMPT_CHARS

        def share(name: str) -> int:
            return max(_MIN_SECTION_CHARS, int(sections.get(name, 0) * ratio))

        return cls(
            total=total,
            source=share("max_source_chars"),
            existing_test=share("max_existing_test_chars"),
            ut_library=share("max_ut_library_chars"),
            api_context=share("max_context_chars"),
            repo_knowledge=share("max_repo_knowledge_chars"),
            knowledge_docs=share("max_knowledge_docs_chars"),
        )

    @classmethod
    def from_settings(cls, settings: LLMSettings | None) -> "_PromptBudgets":
        if settings is None:
            return cls._focused(
                prompt_char_budget(0),
                {
                    "max_source_chars": DEFAULT_MAX_SOURCE_CHARS,
                    "max_existing_test_chars": DEFAULT_MAX_EXISTING_TEST_CHARS,
                    "max_ut_library_chars": DEFAULT_MAX_UT_LIBRARY_CHARS,
                    "max_context_chars": DEFAULT_MAX_CONTEXT_CHARS,
                    "max_repo_knowledge_chars": DEFAULT_MAX_REPO_KNOWLEDGE_CHARS,
                    "max_knowledge_docs_chars": DEFAULT_MAX_KNOWLEDGE_DOCS_CHARS,
                },
            )
        return cls._focused(settings.prompt_char_budget(), settings.effective_budgets())

    @classmethod
    def for_context(cls, context_tokens: int, max_output_tokens: int) -> "_PromptBudgets":
        """Budgets for a window the gateway just told us about."""
        return cls._focused(
            prompt_char_budget(context_tokens, max_output_tokens),
            budgets_for_context(context_tokens, max_output_tokens),
        )

    @staticmethod
    def scaled(value: int, scale: float) -> int:
        if scale >= 1.0:
            return value
        return max(_MIN_SECTION_CHARS, int(value * scale))


def generate_proposals(
    gaps: list[QualityGap],
    adapter: LanguageAdapter,
    env: EnvironmentInfo,
    conventions: TestConventions,
    test_files: list[TestFile],
    client: LLMClient,
    test_dir: Path | None,
    root: Path,
    max_tests_per_file: int = 5,
    llm_settings: LLMSettings | None = None,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    scan: ScanResult | None = None,
    ut_knowledge: UTKnowledge | None = None,
    repo_knowledge: RepoKnowledge | None = None,
    knowledge_docs: KnowledgeDocs | None = None,
) -> list[ProposedFileChange]:
    """Generate one ProposedFileChange per gap (existing or new test file)."""
    proposals: list[ProposedFileChange] = []
    for gap in gaps:
        try:
            change = generate_proposal_for_gap(
                gap=gap,
                adapter=adapter,
                env=env,
                conventions=conventions,
                test_files=test_files,
                client=client,
                test_dir=test_dir,
                root=root,
                max_tests_per_file=max_tests_per_file,
                llm_settings=llm_settings,
                progress=progress,
                cancelled=cancelled,
                scan=scan,
                ut_knowledge=ut_knowledge,
                repo_knowledge=repo_knowledge,
                knowledge_docs=knowledge_docs,
            )
        except GenerationError as exc:
            # Batch mode keeps going, but the reason must not disappear.
            trace(f"GENERATE skipped {gap.source_file.name}: {exc}")
            if progress is not None:
                progress(f"Skipped {gap.source_file.name}: {str(exc).splitlines()[0]}")
            continue
        if change is not None:
            proposals.append(change)
    return proposals


def generate_proposal_for_gap(
    gap: QualityGap,
    adapter: LanguageAdapter,
    env: EnvironmentInfo,
    conventions: TestConventions,
    test_files: list[TestFile],
    client: LLMClient,
    test_dir: Path | None,
    root: Path,
    max_tests_per_file: int = 5,
    llm_settings: LLMSettings | None = None,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    scan: ScanResult | None = None,
    ut_knowledge: UTKnowledge | None = None,
    repo_knowledge: RepoKnowledge | None = None,
    knowledge_docs: KnowledgeDocs | None = None,
    target_test_file: Path | None = None,
) -> ProposedFileChange | None:
    """Generate a ProposedFileChange for a single quality gap.

    ``target_test_file`` overrides where the tests are written (chosen by the user).
    """
    raise_if_cancelled(cancelled)
    if conventions is None:
        conventions = TestConventions()
    if progress is not None:
        progress(f"Generating tests for {gap.source_file.name}...")
    trace(f"GENERATE start file={gap.source_file.name}")

    source_code = read_text_safe(gap.source_file)
    if source_code is None:
        trace(f"GENERATE abort: cannot read source {gap.source_file}")
        raise GenerationError(
            f"Could not read '{gap.source_file.name}'.\n\n"
            f"Path: {gap.source_file}\n\n"
            "The file was moved, is locked by another program, or uses an "
            "unsupported encoding. Re-run Scan & Analyze."
        )

    target = resolve_target_test_file(
        gap.source_file,
        adapter,
        conventions,
        test_files,
        test_dir,
        root,
        override=target_test_file,
    )
    framework = target.framework or env.test_framework

    # Sized once, up front: every context collector below fills exactly the room
    # it will get in the prompt, so nothing is gathered only to be thrown away.
    prompt_budgets = _PromptBudgets.from_settings(llm_settings)

    template = derive_test_template(
        target.path, gap.source_file, test_files, adapter.language_id
    )
    if not template.is_empty:
        trace(f"GENERATE test template {template.describe()}")

    header = adapter.new_test_header(target.path, gap.source_file, env, template)
    header_path = header[0] if header is not None else None
    header_content_template = header[1] if header is not None else ""

    # The top of a brand new test file: includes plus the fixture lifecycle bodies
    # this project defines there. Built up front so the model can fill the bodies.
    preamble_template = ""
    fixture_template = ""
    if target.is_new_file:
        preamble_template = adapter.new_file_preamble(
            gap.source_file, env, target.path, template
        )
        # Only worth its own prompt section when there are real bodies to fill.
        # A preamble that is just imports/includes has nothing for the model to
        # do, and the section would only eat into the context window.
        if header_path is not None and "{" in preamble_template:
            fixture_template = preamble_template

    related = _related_files(gap.source_file, adapter)
    context = collect_symbol_context(
        source=gap.source_file,
        language=adapter.language_id,
        source_code=source_code,
        scan=scan,
        related_contents=[content for _, content in related],
        skip_paths={gap.source_file.with_name(name) for name, _ in related},
        declarations=repo_knowledge.declarations if repo_knowledge is not None else None,
        max_total_chars=prompt_budgets.api_context,
    )
    if context.files:
        trace(
            f"GENERATE api context files={len(context.files)} "
            f"symbols={len(context.symbols)}"
        )
    constructors = _collect_constructors(
        gap.source_file, source_code, related, context, adapter.language_id
    )
    if constructors:
        trace(f"GENERATE constructors known={len(constructors)}")
    repo_context = ""
    seed = "\n".join(
        [source_code, *(content for _, content in related), *gap.untested_functions]
    )
    if repo_knowledge is not None and not repo_knowledge.is_empty():
        repo_context = repo_knowledge.prompt_text_for(
            seed,
            max_chars=prompt_budgets.repo_knowledge,
        )
        if repo_context:
            trace(f"GENERATE repo knowledge chars={len(repo_context)}")
    docs_context = ""
    if knowledge_docs is not None and not knowledge_docs.is_empty():
        docs_context = knowledge_docs.prompt_text(
            seed_text=seed,
            max_chars=prompt_budgets.knowledge_docs,
        )
        if docs_context:
            trace(f"GENERATE knowledge files chars={len(docs_context)}")

    knowledge_index = (
        knowledge_docs.index_path() if knowledge_docs is not None else None
    )
    file_map = build_file_map(
        gap.source_file,
        root=root,
        target_test_file=target.path,
        target_is_new=target.is_new_file,
        header_file=header_path,
        header_is_new=header_path is not None and not header_path.is_file(),
        related_files=[gap.source_file.with_name(name) for name, _ in related],
        dependency_files=context.file_paths(),
        test_dir=(
            ut_knowledge.test_dir
            if ut_knowledge is not None and ut_knowledge.test_dir is not None
            else test_dir
        ),
        knowledge_files=[knowledge_index] if knowledge_index is not None else [],
    )
    trace(f"GENERATE file map chars={len(file_map)}")

    def build_prompt(
        budgets: _PromptBudgets,
        scale: float,
        tests_wanted: int,
        nudge: str = "",
        minimal: bool = False,
    ) -> str:
        # "minimal" is the last automatic attempt: the model gets the source, the
        # conventions and the output format only. No header/fixture block to write
        # first, no repository context - everything that can distract a small model
        # from simply writing the test blocks is gone.
        prompt = build_generation_prompt(
            source_code=source_code,
            source_name=gap.source_file.name,
            gap=gap,
            env=env,
            conventions=conventions,
            existing_test_content="" if minimal else target.content,
            target_test_file=target.path.name,
            test_framework=framework,
            header_file_name=(
                "" if minimal else (header_path.name if header_path is not None else "")
            ),
            header_template="" if minimal else header_content_template,
            fixture_template="" if minimal else fixture_template,
            related_files=[] if minimal else related,
            file_map=file_map,
            api_context="" if minimal else context.to_prompt_text(),
            repo_context="" if minimal else repo_context,
            repo_knowledge_file=(
                str(repo_knowledge.markdown_path)
                if not minimal
                and repo_knowledge is not None
                and repo_knowledge.markdown_path is not None
                else ""
            ),
            knowledge_docs="" if minimal else docs_context,
            construction_notes=(
                "" if minimal else constructors_prompt_text(constructors)
            ),
            access_notes=adapter.access_notes(gap.source_file, source_code),
            ut_knowledge=None if minimal else ut_knowledge,
            max_tests=tests_wanted,
            max_source_chars=budgets.scaled(budgets.source, scale),
            max_existing_test_chars=budgets.scaled(budgets.existing_test, scale),
            max_ut_library_chars=budgets.scaled(budgets.ut_library, scale),
            max_total_chars=budgets.scaled(budgets.total, scale),
        )
        return f"{prompt}\n\n{nudge}" if nudge else prompt

    model_label = (
        llm_settings.model.strip()
        if llm_settings is not None and llm_settings.model.strip()
        else "the configured model"
    )
    trace(f"GENERATE model={model_label!r}")

    prompt = ""
    response = ""
    header_content = header_content_template
    preamble = preamble_template
    parsed: list[tuple[str, str, bool, str]] = []
    overflow_detail = ""
    scale = 1.0
    tests_wanted = max(1, max_tests_per_file)
    unparsable_retries = 0
    nudge = ""
    minimal_prompt = False
    resized_from_gateway = False
    model_name = llm_settings.model if llm_settings is not None else ""
    max_output = (
        llm_settings.max_output_tokens if llm_settings is not None else 0
    )
    # A window learned earlier (from another file in this run) already applies:
    # the very first prompt is built for the size the gateway really accepts.
    known_window = learned_context_limit(model_name)
    if known_window and llm_settings is not None:
        trace(f"GENERATE using learned context window={known_window}")

    for attempt in range(_MAX_GENERATION_ATTEMPTS):
        raise_if_cancelled(cancelled)
        prompt = build_prompt(
            prompt_budgets, scale, tests_wanted, nudge, minimal_prompt
        )
        trace(
            f"GENERATE prompt chars={len(prompt)} scale={scale:.2f} "
            f"tests={tests_wanted} attempt={attempt + 1}"
        )
        if progress is not None:
            progress(f"Calling {model_label} for {gap.source_file.name}...")
        try:
            response = client.generate(
                prompt,
                system=SYSTEM_PROMPT,
                cancelled=cancelled,
                progress=progress,
            )
        except ContextLengthError as exc:
            overflow_detail = str(exc)
            reported = getattr(exc, "limit_tokens", 0)
            trace(
                f"GENERATE context overflow scale={scale:.2f} "
                f"reported_window={reported}: {exc}"
            )
            last_attempt = attempt == _MAX_GENERATION_ATTEMPTS - 1
            if reported and llm_settings is not None and not resized_from_gateway:
                # The gateway told us its real window: rebuild the prompt for
                # that size instead of guessing a smaller fraction.
                remember_context_limit(model_name, reported)
                prompt_budgets = _PromptBudgets.for_context(reported, max_output)
                resized_from_gateway = True
                scale = 1.0
                if progress is not None:
                    progress(
                        f"The model's real context window is {reported} tokens; "
                        "rebuilding the prompt to fit it..."
                    )
                continue
            if not reported and model_name:
                # The gateway refused without naming its window. The prompt it
                # just rejected is, by definition, bigger than that window, so
                # remember it as an upper bound: every later file in this run
                # starts from a size that has a chance to fit instead of
                # repeating the same overflow.
                remember_context_limit(model_name, estimate_tokens(len(prompt)))
            next_scale = scale * _BLIND_SHRINK
            if last_attempt or next_scale < _MIN_PROMPT_SCALE:
                raise GenerationError(
                    _too_large_message(
                        source_name=gap.source_file.name,
                        prompt=prompt,
                        settings=llm_settings,
                        detail=overflow_detail,
                    )
                ) from exc
            scale = next_scale
            tests_wanted = max(_MIN_TESTS_ON_RETRY, tests_wanted - 1)
            if progress is not None:
                progress(
                    "Prompt did not fit the model's context window; "
                    "retrying with less project context..."
                )
            continue
        trace(f"GENERATE llm response chars={len(response)}")
        raise_if_cancelled(cancelled)

        header_content = header_content_template
        body = response
        if header_path is not None:
            generated_header, body = parse_ut_header_response(response)
            if generated_header:
                header_content = _ensure_required_includes(
                    header_content_template, generated_header
                )
                header_content = _ensure_test_accessor(
                    header_content_template, header_content
                )
                header_content = _fix_null_initializers(
                    header_content, adapter.language_id
                )
                trace(f"GENERATE ut header chars={len(header_content)}")
        preamble = preamble_template
        if fixture_template:
            generated_fixture, body = parse_ut_fixture_response(body)
            if generated_fixture:
                preamble = _ensure_required_includes(
                    fixture_template, generated_fixture
                )
                preamble = _fix_null_initializers(preamble, adapter.language_id)
                trace(f"GENERATE ut fixture chars={len(preamble)}")
        parsed = parse_generation_response(body, language=adapter.language_id)
        trace(f"GENERATE parsed tests={len(parsed)}")
        if parsed:
            break
        unparsable_retries += 1
        if unparsable_retries > _UNPARSABLE_RETRY_LIMIT:
            break
        # A very large prompt and a long answer are the two common reasons a model
        # replies without any test block: ask for less of both and try once more.
        scale = max(_MIN_PROMPT_SCALE, scale * _BLIND_SHRINK)
        tests_wanted = max(_MIN_TESTS_ON_RETRY, tests_wanted - 1)
        nudge = _RETRY_NUDGE
        minimal_prompt = unparsable_retries >= _MINIMAL_PROMPT_AFTER_RETRIES
        trace(
            "GENERATE no tests parsed; retrying with a smaller prompt "
            f"(minimal={minimal_prompt})"
        )
        if progress is not None:
            progress(
                f"{model_label} replied without tests; retrying with "
                + (
                    "the source code and the output format only..."
                    if minimal_prompt
                    else f"a smaller prompt and {tests_wanted} tests..."
                )
            )

    if not parsed:
        preview = response.strip().replace("\n", " ")[:240]
        trace(f"GENERATE abort: parser found no tests; response preview={preview!r}")
        raise GenerationError(
            _no_tests_message(
                source_name=gap.source_file.name,
                response=response,
                prompt=prompt,
                model=llm_settings.model if llm_settings is not None else "",
                settings=llm_settings,
            ),
            response_preview=response.strip()[:1500],
        )

    tests = [
        ProposedTest(
            name=name,
            code=flag_unknown_constructions(
                _fix_null_initializers(code, adapter.language_id),
                constructors,
                adapter.language_id,
            ),
            is_edge_case=edge,
            rationale=rationale,
        )
        for name, code, edge, rationale in parsed[:max_tests_per_file]
    ]
    used_code = "\n".join(test.code for test in tests)
    if header_content:
        header_content = flag_unknown_constructions(
            header_content, constructors, adapter.language_id
        )
        header_content = ensure_symbol_includes(
            header_content, used_code + "\n" + header_content, context, adapter.language_id
        )

    if not target.is_new_file:
        return ProposedFileChange(
            path=target.path,
            language=adapter.language_id,
            is_new_file=False,
            original_content=target.content,
            tests=tests,
            source_file=gap.source_file,
            header_path=header_path,
            header_content=header_content,
        )

    preamble = flag_unknown_constructions(preamble, constructors, adapter.language_id)
    if header_path is None:
        preamble = ensure_symbol_includes(
            preamble, used_code, context, adapter.language_id
        )
    return ProposedFileChange(
        path=target.path,
        language=adapter.language_id,
        is_new_file=True,
        original_content="",
        preamble=preamble,
        tests=tests,
        source_file=gap.source_file,
        header_path=header_path,
        header_content=header_content,
    )


def _no_tests_message(
    *,
    source_name: str,
    response: str,
    prompt: str,
    model: str,
    settings: LLMSettings | None = None,
) -> str:
    """Short failure text: why it failed and what to change in Settings."""
    stripped = response.strip()
    model_label = model.strip() or "the model"
    window = settings.context_tokens if settings is not None else 0
    output = settings.max_output_tokens if settings is not None else 0

    if not stripped:
        return (
            f"No tests for '{source_name}': {model_label} returned nothing.\n\n"
            "Settings:\n"
            "- Generation timeout: raise it (the model may still be loading).\n"
            "- Max tests per file: 3.\n\n"
            "Then press the button again."
        )

    return (
        f"No tests for '{source_name}': {model_label} replied {len(stripped)} "
        f"characters without any test code (prompt {len(prompt)} chars).\n\n"
        "Settings:\n"
        "- Max tests per file: 3.\n"
        f"- Max output tokens: 4096"
        + (f" (now {output}).\n" if output else ".\n")
        + "- Model context window: the window the model REALLY has"
        + (f" (now {window} tokens).\n" if window else ".\n")
        + "- Thinking: tick 'Ask the model not to think out loud'.\n\n"
        "Then press the button again.\n\n"
        f"Model said: {_clip(stripped, 300)}"
    )


def _too_large_message(
    *,
    source_name: str,
    prompt: str,
    settings: LLMSettings | None,
    detail: str,
) -> str:
    """Short failure text for a request that never fit the context window."""
    window = settings.context_tokens if settings is not None else 0
    reply = settings.output_token_budget() if settings is not None else 0
    reported = learned_context_limit(settings.model) if settings is not None else 0
    window_fix = (
        f"- Model context window: set it to {reported} tokens "
        "(the provider reported this as the real window)."
        if reported
        else "- Model context window: lower it to the window the model REALLY has"
        + (f" (now {window} tokens)." if window else ".")
    )
    message = (
        f"No tests for '{source_name}': the request does not fit the model's "
        f"context window ({len(prompt)} chars on the last try).\n\n"
        "Settings:\n"
        f"{window_fix}\n"
        f"- Max output tokens: lower to 4096"
        + (f" (now {reply}).\n" if reply else ".\n")
        + "- Max tests per file: 3.\n\n"
        "Then press the button again."
    )
    if detail:
        message += f"\n\nProvider: {_clip(detail, 200)}"
    return message


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def _related_files(source: Path, adapter: LanguageAdapter) -> list[tuple[str, str]]:
    """Sibling declaration files (e.g. the C++ header) the tests must compile against."""
    related: list[tuple[str, str]] = []
    for extension in adapter.header_extensions:
        candidate = source.with_suffix(extension)
        if candidate == source or not candidate.is_file():
            continue
        content = read_text_safe(candidate)
        if content:
            related.append((candidate.name, content))
    return related


# ---- constructor grounding -------------------------------------------------


def _collect_constructors(
    source: Path,
    source_code: str,
    related: list[tuple[str, str]],
    context: SymbolContext,
    language: str,
) -> list[ConstructorInfo]:
    """Every constructor visible to the tests: source, its headers, its dependencies."""
    collected: list[ConstructorInfo] = []
    for name, content in [(source.name, source_code), *related]:
        path = source.with_name(name)
        collected.extend(
            extract_constructors(
                condense_declarations(content, language),
                language,
                file_name=name,
                include=include_directive_for(path, language),
            )
        )
    collected.extend(context.constructors)

    unique: list[ConstructorInfo] = []
    seen: set[tuple[str, str]] = set()
    for item in collected:
        key = (item.type_name, re.sub(r"\s+", "", item.signature))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


_NULL_INIT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<decl>(?:(?:static|mutable|inline)\s+)*"
    r"[A-Za-z_][\w:]*(?:\s*<[^;=]*>)?(?:\s*::\s*[A-Za-z_]\w*)*)"
    r"\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?:nullptr|NULL)\s*;[ \t]*$"
)
_SMART_POINTER_HINT = re.compile(r"(?:unique_ptr|shared_ptr|weak_ptr|auto_ptr|_ptr\b|Ptr\b)")


def _fix_null_initializers(code: str, language: str) -> str:
    """Drop ``= nullptr`` from C++ declarations whose type cannot hold null.

    Models often emit ``Foo obj = nullptr;`` for a value type and then use ``obj``;
    that never compiles. Pointers and smart pointers are left untouched.
    """
    if language != "cpp" or not code:
        return code
    lines = code.splitlines()
    changed = False
    for index, line in enumerate(lines):
        match = _NULL_INIT_RE.match(line)
        if match is None:
            continue
        decl = match.group("decl")
        if decl.split()[-1] == "auto" or _SMART_POINTER_HINT.search(decl):
            continue
        lines[index] = f"{match.group('indent')}{decl} {match.group('name')};"
        changed = True
    if not changed:
        return code
    return "\n".join(lines) + ("\n" if code.endswith("\n") else "")


def _ensure_required_includes(template: str, generated: str) -> str:
    """Re-insert any mandatory ``#include`` from the skeleton the model dropped."""
    missing = [
        line.strip()
        for line in template.splitlines()
        if line.strip().startswith("#include") and line.strip() not in generated
    ]
    if not missing:
        return generated
    lines = generated.splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if stripped.startswith("#"):
            insert_at = index + 1
            continue
        break
    return "\n".join(lines[:insert_at] + missing + lines[insert_at:]) + "\n"


def _ensure_test_accessor(template: str, generated: str) -> str:
    """Re-insert the protected-member accessor class when the model dropped it."""
    match = re.search(r"class\s+(\w+TestAccess)\s*:", template)
    if match is None or match.group(1) in generated:
        return generated
    block = _accessor_block(template, match.group(1))
    if not block:
        return generated
    lines = generated.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "protected:":
            return "\n".join(lines[: index + 1] + block + lines[index + 1:]) + "\n"
    return generated


def _accessor_block(template: str, name: str) -> list[str]:
    """Lines of the accessor class in the skeleton, including its leading comments."""
    lines = template.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.search(rf"class\s+{name}\s*:", line)),
        None,
    )
    if start is None:
        return []
    while start > 0 and lines[start - 1].strip().startswith("//"):
        start -= 1
    for index in range(start, len(lines)):
        if lines[index].strip() == "};":
            return lines[start: index + 1] + [""]
    return []
