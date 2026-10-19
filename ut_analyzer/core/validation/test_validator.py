"""Validation pipeline for generated tests.

Per test: syntax check -> duplicate detection -> compile -> run.
Compile/run require writing the composed file temporarily; the original file
is always restored afterwards (validation must never leave changes on disk).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ...adapters.base import LanguageAdapter
from ...models import (
    EnvironmentInfo,
    ProposedFileChange,
    ProposedTest,
    TestFile,
    ValidationResult,
    ValidationStatus,
)
from ...utils.fs import restore_backup, write_text_atomic
from ...utils.parsing import normalize_code, similarity
from ..cancellation import raise_if_cancelled
from ..diffing.diff_engine import compose_content

_DUPLICATE_THRESHOLD = 0.9


def validate_proposals(
    changes: list[ProposedFileChange],
    adapter: LanguageAdapter,
    env: EnvironmentInfo,
    existing_tests: list[TestFile],
    root: Path,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Validate every proposed test in place (fills test.validation)."""
    existing_bodies = [
        (case.name, normalize_code(case.body))
        for tf in existing_tests
        for case in tf.cases
        if case.body
    ]
    existing_names = {name for name, _ in existing_bodies}

    for change in changes:
        for test in change.tests:
            raise_if_cancelled(cancelled)
            if progress is not None:
                progress(f"Validating {test.name}...")
            test.validation = _validate_single(
                test, change, adapter, env, root, existing_bodies, existing_names
            )
            # A validated test's name joins the duplicate pool for later tests.
            existing_names.add(test.name)
            existing_bodies.append((test.name, normalize_code(test.effective_code)))


def _validate_single(
    test: ProposedTest,
    change: ProposedFileChange,
    adapter: LanguageAdapter,
    env: EnvironmentInfo,
    root: Path,
    existing_bodies: list[tuple[str, str]],
    existing_names: set[str],
) -> ValidationResult:
    # 1. Duplicate detection (cheap, no tooling required).
    duplicate = _find_duplicate(test, existing_bodies, existing_names)
    if duplicate is not None:
        return ValidationResult(
            status=ValidationStatus.DUPLICATE,
            message=f"matches existing test '{duplicate}'",
        )

    # 2. Syntax check on the fully composed file (context-accurate).
    composed = compose_content(change, adapter, include=[test])
    ok, message = adapter.check_syntax(composed)
    if not ok:
        return ValidationResult(status=ValidationStatus.SYNTAX_ERROR, message=message)

    # 3 + 4. Compile and run, with the composed file temporarily on disk.
    if not env.build_command and not env.test_command:
        return ValidationResult(
            status=ValidationStatus.NOT_VALIDATED,
            message="no build or test command detected for this project",
        )

    existed_before = change.path.exists()
    backup = None
    header_written = False
    try:
        if (
            change.header_path is not None
            and change.header_content
            and not change.header_path.exists()
        ):
            write_text_atomic(change.header_path, change.header_content, backup=False)
            header_written = True
        backup = write_text_atomic(change.path, composed, backup=existed_before)

        compile_result = adapter.compile_test_target(root, env, change.path)
        if compile_result is not None:
            if compile_result.exit_code is None:
                return ValidationResult(
                    status=ValidationStatus.NOT_VALIDATED,
                    message=compile_result.error or "compilation could not be started",
                    details=compile_result.output,
                )
            if not compile_result.ok:
                return ValidationResult(
                    status=ValidationStatus.COMPILE_ERROR,
                    message="the test target failed to compile",
                    details=_tail(compile_result.output),
                )

        run_result = adapter.run_single_test(root, env, change.path, test.name)
        if run_result is None:
            if compile_result is not None and compile_result.ok:
                return ValidationResult(
                    status=ValidationStatus.NOT_VALIDATED,
                    message="compiled successfully, but the test could not be executed "
                    "(no way to run a single test was found)",
                )
            return ValidationResult(
                status=ValidationStatus.NOT_VALIDATED,
                message="the required test tooling is not available on this machine",
            )
        if run_result.exit_code is None:
            return ValidationResult(
                status=ValidationStatus.NOT_VALIDATED,
                message=run_result.error or "the test run could not be started",
                details=_tail(run_result.output),
            )
        if not run_result.ok:
            return ValidationResult(
                status=ValidationStatus.TEST_FAILED,
                message="the test compiled but failed when executed",
                details=_tail(run_result.output),
            )
        return ValidationResult(status=ValidationStatus.VALIDATED)
    except OSError as exc:
        return ValidationResult(
            status=ValidationStatus.NOT_VALIDATED,
            message=f"could not write the candidate file: {exc}",
        )
    finally:
        restore_backup(change.path, backup, existed_before)
        if header_written and change.header_path is not None:
            restore_backup(change.header_path, None, existed_before=False)


def _find_duplicate(
    test: ProposedTest,
    existing_bodies: list[tuple[str, str]],
    existing_names: set[str],
) -> str | None:
    if test.name in existing_names:
        return test.name
    normalized = normalize_code(test.effective_code)
    if not normalized:
        return None
    for name, body in existing_bodies:
        if body and similarity(normalized, body) >= _DUPLICATE_THRESHOLD:
            return name
    return None


def _tail(output: str, limit: int = 3000) -> str:
    return output[-limit:] if len(output) > limit else output
