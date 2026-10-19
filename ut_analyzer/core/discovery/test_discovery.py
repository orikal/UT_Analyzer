"""Unit-test discovery: parses every discovered test file into structured cases."""

from __future__ import annotations

from collections.abc import Callable

from ...adapters.base import LanguageAdapter
from ...core.cancellation import raise_if_cancelled
from ...models import ScanResult, TestFile
from ...utils.fs import read_text_safe

#: One progress message per parsed file floods the UI thread on a large repo.
_PROGRESS_EVERY = 20


def discover_tests(
    scan: ScanResult,
    adapter: LanguageAdapter,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[TestFile]:
    """Parse all test files of the primary language into TestFile models."""
    test_files: list[TestFile] = []
    scanned_tests = scan.test_files(adapter.language_id)
    for position, scanned in enumerate(scanned_tests):
        raise_if_cancelled(cancelled)
        content = read_text_safe(scanned.path)
        if content is None:
            continue
        if progress is not None and position % _PROGRESS_EVERY == 0:
            progress(
                f"Reading test files ({position}/{len(scanned_tests)}): "
                f"{scanned.path.name}"
            )
        parsed = adapter.parse_test_file(scanned.path, content)
        test_files.append(parsed)
    return test_files
