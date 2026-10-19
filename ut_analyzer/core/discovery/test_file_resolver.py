"""Resolve the canonical unit-test file path for a given source file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ...adapters.base import LanguageAdapter
from ...models import TestConventions, TestFile
from ...utils.fs import read_text_safe
from ..detection.test_content_sampler import planned_test_directory
from .naming_convention import (
    TEST_STEM_MARKERS,
    NamingConvention,
    apply_pattern,
    detect_naming_convention,
    source_stem_from_test_stem,
    split_test_stem,
)

__all__ = [
    "TEST_STEM_MARKERS",
    "TargetTestFile",
    "TestFileIndex",
    "find_matching_test_file",
    "resolve_target_test_file",
    "source_stem_from_test_stem",
    "split_test_stem",
]


@dataclass(frozen=True)
class TargetTestFile:
    """Where generated tests for a source file should be written."""

    path: Path
    content: str
    is_new_file: bool
    framework: str | None = None
    #: True when the project uses several naming conventions and the choice of
    #: ``path`` is a guess the user should confirm.
    ambiguous: bool = False
    #: Other paths that would be valid under the competing conventions.
    alternatives: tuple[Path, ...] = ()
    #: Human-readable explanation of how ``path`` was chosen.
    reason: str = ""


def resolve_target_test_file(
    source: Path,
    adapter: LanguageAdapter,
    conventions: TestConventions | None,
    test_files: list[TestFile],
    test_dir: Path | None,
    root: Path,
    override: Path | None = None,
    index: "TestFileIndex | None" = None,
) -> TargetTestFile:
    """Resolve the UT file for *source* (existing discovered, on disk, or canonical new path).

    ``override`` is a destination picked by the user and always wins.
    ``index`` is a prepared :class:`TestFileIndex`; pass it when resolving many
    source files so the test files are only indexed once.
    """
    index = index if index is not None else TestFileIndex(test_files)
    if override is not None:
        disk_content = read_text_safe(override)
        return TargetTestFile(
            path=override,
            content=disk_content or "",
            is_new_file=disk_content is None,
            framework=index.framework_of(override),
            reason="the destination was chosen by the user",
        )

    naming = conventions.file_naming if conventions is not None else None
    target_dir = _target_dir(source, test_dir, root)
    canonical_path = target_dir / adapter.test_file_name_for(source, naming)
    new_file_path = target_dir / adapter.new_test_file_name_for(source, naming)

    matched = index.match(source)
    if matched is not None:
        return TargetTestFile(
            path=matched.path,
            content=matched.content,
            is_new_file=False,
            framework=matched.framework,
            reason="an existing test file already covers this source file",
        )

    for existing in (new_file_path, canonical_path):
        disk_content = read_text_safe(existing)
        if disk_content is not None:
            return TargetTestFile(
                path=existing,
                content=disk_content,
                is_new_file=False,
                framework=None,
                reason="a test file with the conventional name already exists on disk",
            )

    convention = index.convention
    alternatives = tuple(
        target_dir / apply_pattern(pattern, source)
        for pattern in convention.alternatives
        if target_dir / apply_pattern(pattern, source) != new_file_path
    )
    return TargetTestFile(
        path=new_file_path,
        content="",
        is_new_file=True,
        framework=None,
        ambiguous=convention.ambiguous,
        alternatives=alternatives,
        reason=convention.describe(),
    )


class TestFileIndex:
    """Lookups over the project's test files, computed once.

    Resolving a target used to rescan every test file - and re-derive the
    naming convention from all of them - for every single source file. On a
    large repository that quadratic work is what makes the quality analysis
    look frozen, so the whole-set work is done here exactly once.
    """

    __slots__ = ("test_files", "convention", "_by_path", "_by_implied", "_stems", "_matches")

    def __init__(self, test_files: Sequence[TestFile]) -> None:
        self.test_files = list(test_files)
        self.convention: NamingConvention = detect_naming_convention(
            [tf.path for tf in self.test_files]
        )
        self._by_path: dict[str, TestFile] = {}
        self._by_implied: dict[str, TestFile] = {}
        #: (test stem, implied source stem, file) for the substring fallbacks
        self._stems: list[tuple[str, str | None, TestFile]] = []
        self._matches: dict[str, TestFile | None] = {}
        for test_file in self.test_files:
            self._by_path.setdefault(str(test_file.path), test_file)
            stem = test_file.path.stem.lower()
            implied = source_stem_from_test_stem(stem)
            if implied is not None:
                self._by_implied.setdefault(implied, test_file)
            self._stems.append((stem, implied, test_file))

    def framework_of(self, path: Path) -> str | None:
        """Framework of the already-parsed test file at *path*, if it is one of them."""
        test_file = self._by_path.get(str(path))
        return test_file.framework if test_file is not None else None

    def match(self, source: Path) -> TestFile | None:
        """The discovered test file that covers *source*, if any."""
        source_stem = source.stem.lower()
        if source_stem in self._matches:
            return self._matches[source_stem]
        matched = self._by_implied.get(source_stem)
        if matched is None:
            matched = self._best_partial(source_stem)
        self._matches[source_stem] = matched
        return matched

    def _best_partial(self, source_stem: str) -> TestFile | None:
        """Fallback scan for test files that only mention the source stem."""
        best: TestFile | None = None
        best_score = 0
        for stem, implied, test_file in self._stems:
            if implied is not None and source_stem in implied:
                score = 50
            elif source_stem in stem:
                score = 10
            else:
                continue
            if score > best_score:
                best_score = score
                best = test_file
                if best_score == 50:
                    break  # the strongest verdict this fallback can reach
        return best


def find_matching_test_file(source: Path, test_files: list[TestFile]) -> TestFile | None:
    """Return the discovered test file that covers *source*, if any.

    Prefer :meth:`TestFileIndex.match` when several source files are resolved.
    """
    return TestFileIndex(test_files).match(source)


def _target_dir(source: Path, test_dir: Path | None, root: Path) -> Path:
    if test_dir is not None and test_dir.is_dir():
        return test_dir
    return planned_test_directory(root, test_dir)
