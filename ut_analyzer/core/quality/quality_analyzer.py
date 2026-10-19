"""Test quality analysis: strength metrics, weak spots and concrete gaps."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ...adapters.base import LanguageAdapter
from ...core.cancellation import raise_if_cancelled
from ...core.discovery.test_file_resolver import (
    TestFileIndex,
    resolve_target_test_file,
    source_stem_from_test_stem,
)
from ...core.quality.coverage_map import (
    CoverageConfidence,
    FileCoverage,
    TestCoverageMapper,
)
from ...core.semantics import extract_file
from ...core.semantics.symbol_resolver import SymbolResolver
from ...models import (
    GAP_ADDITIONAL_CASES,
    GAP_NO_TEST_FILE,
    GAP_UNTESTED_FUNCTIONS,
    EnvironmentInfo,
    FileSemantics,
    ProjectConfig,
    QualityGap,
    QualityReport,
    ScanResult,
    ScannedFile,
    TestConventions,
    TestFile,
)
from ...utils.fs import read_text_safe

#: A source file rarely has more than one real header; the cap keeps a repo with
#: many same-named headers from dragging unrelated declarations in.
_MAX_COMPANION_HEADERS = 2

#: Progress is reported every N files: one message per file would flood the UI
#: thread on a large repository, no message at all makes the step look frozen.
_PROGRESS_EVERY = 20


def analyze_quality(
    scan: ScanResult,
    test_files: list[TestFile],
    adapter: LanguageAdapter,
    env: EnvironmentInfo,
    config: ProjectConfig,
    conventions: TestConventions | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
) -> QualityReport:
    report = QualityReport()
    source_files = scan.source_files(adapter.language_id)
    report.total_source_files = len(source_files)
    report.total_test_files = len(test_files)

    all_cases = [c for tf in test_files for c in tf.cases]
    report.total_test_cases = len(all_cases)
    report.tests_without_assertions = sum(1 for c in all_cases if c.assertion_count == 0)
    if all_cases:
        report.avg_assertions_per_test = round(
            sum(c.assertion_count for c in all_cases) / len(all_cases), 2
        )

    report.trivial_tests = [
        f"{c.file.name if c.file else '?'}::{c.name}"
        for c in all_cases
        if c.assertion_count == 0
    ][:30]

    tested_stems = _tested_source_stems(test_files)
    focus_keys = directory_keys(config.focus_dirs)

    language = adapter.language_id
    test_semantics: dict[Path, FileSemantics] = {}
    for position, test_file in enumerate(test_files):
        raise_if_cancelled(cancelled)
        if progress is not None and position % _PROGRESS_EVERY == 0:
            progress(
                f"Reading the existing tests ({position}/{len(test_files)}): "
                f"{test_file.path.name}"
            )
        test_semantics[test_file.path] = extract_file(
            test_file.path, language, test_file.content
        )
    test_index = TestFileIndex(test_files)
    resolver = SymbolResolver(language)
    resolver.add_all(test_semantics.values())
    mapper = TestCoverageMapper(resolver)
    header_cache: dict[Path, FileSemantics] = {}
    header_index = _header_index(source_files, adapter)
    # Only a .cpp that is itself analyzed can stand in for its header; when it
    # sits outside the selected folders the header must be analyzed instead,
    # otherwise the whole class disappears from the report.
    implemented_stems = {
        f.path.stem.lower()
        for f in source_files
        if not _is_header_source(f.path, adapter)
        and not _is_trivial_source(f.path)
        and in_focus(f.path, focus_keys)
    }

    analyzed = 0
    in_scope = sum(1 for f in source_files if in_focus(f.path, focus_keys))
    for scanned in source_files:
        raise_if_cancelled(cancelled)
        if not in_focus(scanned.path, focus_keys):
            continue
        if progress is not None and analyzed % _PROGRESS_EVERY == 0:
            progress(
                f"Analyzing test quality ({analyzed}/{in_scope}): {scanned.path.name}"
            )
        analyzed += 1
        if _is_trivial_source(scanned.path):
            continue
        if _is_header_source(scanned.path, adapter) and (
            scanned.path.stem.lower() in implemented_stems
        ):
            # The .cpp of this header is analyzed instead, headers included.
            continue
        stem = scanned.path.stem.lower()
        has_test_file = stem in tested_stems
        content = read_text_safe(scanned.path)
        if content is None:
            continue

        source_semantics = extract_file(scanned.path, language, content)
        resolver.add(source_semantics)
        if not source_semantics.parse_ok:
            report.unparsed_files.append(_rel(scanned.path, scan.root))

        # A C++ class declares its API in the companion header, so the .cpp alone
        # never lists every function the user may want tests for.
        headers = _companion_header_semantics(
            scanned.path, adapter, language, header_cache, header_index
        )
        for header in headers:
            resolver.add(header)
        declared = _with_header_symbols(source_semantics, headers)

        target = resolve_target_test_file(
            scanned.path,
            adapter,
            conventions,
            test_files,
            config.test_dir,
            config.root,
            index=test_index,
        )
        matched_test = test_index.match(scanned.path)
        related = _related_test_semantics(matched_test, target, test_semantics)
        existing_test_count = len(matched_test.cases) if matched_test is not None else 0

        coverage = mapper.map_file(declared, related, include_non_public=True)
        coverage = _declared_in_headers(coverage, headers)
        if not coverage.entries:
            continue
        untested = coverage.untested_names(public_only=True)
        uncertain = coverage.uncertain_names(public_only=True)
        notes = {e.qualified_name: e.reason for e in coverage.entries if e.reason}
        all_names = coverage.all_names()
        states = _function_states(coverage)
        non_public = coverage.non_public_names()

        if not has_test_file and not matched_test and target.is_new_file:
            report.files_without_tests.append(_rel(scanned.path, scan.root))
            report.gaps.append(
                _gap(
                    scanned.path,
                    "no test file covers this source file",
                    GAP_NO_TEST_FILE,
                    untested or coverage.all_names(public_only=True),
                    uncertain,
                    notes,
                    target,
                    existing_test_count,
                    all_names,
                    states,
                    non_public,
                )
            )
            continue

        report.files_with_tests.append(_rel(scanned.path, scan.root))
        if untested:
            reason = f"{len(untested)} public symbols have no test that calls them"
            kind = GAP_UNTESTED_FUNCTIONS
        else:
            reason = (
                "every public symbol is called by a test; additional cases can "
                "still strengthen edge-case coverage"
            )
            kind = GAP_ADDITIONAL_CASES
        report.gaps.append(
            _gap(
                scanned.path,
                reason,
                kind,
                untested,
                uncertain,
                notes,
                target,
                existing_test_count,
                all_names,
                states,
                non_public,
            )
        )

    report.gaps.sort(
        key=lambda g: (g.kind == GAP_ADDITIONAL_CASES, len(g.untested_functions))
    )

    if report.total_test_cases == 0:
        report.notes.append("No unit tests were found in the selected folders.")
    return report


def _tested_source_stems(test_files: list[TestFile]) -> set[str]:
    """Source-file stems that appear to have a dedicated test file."""
    stems: set[str] = set()
    for tf in test_files:
        implied = source_stem_from_test_stem(tf.path.stem.lower())
        if implied:
            stems.add(implied)
        stems.add(tf.path.stem.lower())
    return stems


def directory_keys(folders: list[Path]) -> list[str]:
    """Comparable, syscall-free keys for the folders the user marked."""
    keys: list[str] = []
    for folder in folders:
        key = os.path.normcase(os.path.abspath(str(folder)))
        keys.append(key.rstrip(os.sep) + os.sep)
    return keys


def in_focus(path: Path, focus_keys: list[str]) -> bool:
    """True when the file sits inside one of the marked folders.

    No marked folders means the whole project is in focus.
    """
    if not focus_keys:
        return True
    key = os.path.normcase(os.path.abspath(str(path)))
    return any(key.startswith(folder) for folder in focus_keys)


def _is_trivial_source(path: Path) -> bool:
    name = path.name.lower()
    return name in ("__init__.py", "__main__.py", "setup.py", "conftest.py") or name.startswith(
        ("main.", "index.")
    )


def _is_header_source(path: Path, adapter: LanguageAdapter) -> bool:
    return path.suffix.lower() in adapter.header_extensions


def _header_index(
    source_files: list[ScannedFile], adapter: LanguageAdapter
) -> dict[str, list[Path]]:
    """Header files of the repository, grouped by file stem.

    Projects that keep headers in ``include/`` next to ``src/`` would otherwise
    lose every declaration, because the .cpp only defines part of the class.
    """
    index: dict[str, list[Path]] = {}
    for scanned in source_files:
        if _is_header_source(scanned.path, adapter):
            index.setdefault(scanned.path.stem.lower(), []).append(scanned.path)
    return index


def _companion_header_semantics(
    source: Path,
    adapter: LanguageAdapter,
    language: str,
    cache: dict[Path, FileSemantics],
    header_index: dict[str, list[Path]] | None = None,
) -> list[FileSemantics]:
    """Parsed headers that declare the API of ``source`` (``Foo.cpp`` -> ``Foo.h``)."""
    parsed: list[FileSemantics] = []
    for candidate in _header_candidates(source, adapter, header_index):
        cached = cache.get(candidate)
        if cached is None:
            content = read_text_safe(candidate)
            if content is None:
                continue
            cached = extract_file(candidate, language, content)
            cache[candidate] = cached
        parsed.append(cached)
    return parsed


def _header_candidates(
    source: Path,
    adapter: LanguageAdapter,
    header_index: dict[str, list[Path]] | None,
) -> list[Path]:
    """Headers for ``source``: same folder first, then same stem anywhere."""
    if _is_header_source(source, adapter):
        return []
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen and path != source:
            seen.add(key)
            candidates.append(path)

    for extension in adapter.header_extensions:
        sibling = source.with_suffix(extension)
        if sibling != source and sibling.is_file():
            add(sibling)
    for path in (header_index or {}).get(source.stem.lower(), ()):
        add(path)
    return candidates[:_MAX_COMPANION_HEADERS]


def _with_header_symbols(
    source: FileSemantics, headers: list[FileSemantics]
) -> FileSemantics:
    """``source`` plus the declarations of its headers, headers first.

    The in-class declaration carries the access specifier, while the
    out-of-line definition in the .cpp does not, so the header must come first
    for private/protected members to be recognised as such.
    """
    if not headers:
        return source
    extra = [s for header in headers for s in header.symbols]
    if not extra:
        return source
    return replace(source, symbols=[*extra, *source.symbols])


def _declared_in_headers(
    coverage: FileCoverage, headers: list[FileSemantics]
) -> FileCoverage:
    """Keep only the symbols the companion header declares.

    ``x.cpp`` also defines file-local helpers and out-of-line bodies of symbols
    that belong to other translation units; the API a unit test can call is the
    one ``x.h`` declares, and that is what the user picks from. Deduplication
    already prefers the in-class declaration, so a method defined out of line is
    reported with its header location.

    Falls back to the unfiltered coverage when the header declares nothing
    testable, so a header-less or free-function file never disappears.
    """
    if not headers:
        return coverage
    header_keys = {_path_key(h.path) for h in headers if h.path is not None}
    kept = [
        entry
        for entry in coverage.entries
        if entry.symbol.file is not None and _path_key(entry.symbol.file) in header_keys
    ]
    if not kept:
        return coverage
    return FileCoverage(source=coverage.source, entries=kept)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _related_test_semantics(matched_test, target, index):
    """Parsed tests that could plausibly cover the source file.

    Only the resolved test file counts; scanning every test in the project
    would reintroduce the false positives the name-matching heuristic caused.
    """
    related = []
    if matched_test is not None and matched_test.path in index:
        related.append(index[matched_test.path])
    if target.path is not None and target.path in index:
        semantics = index[target.path]
        if semantics not in related:
            related.append(semantics)
    return related


def _gap(
    source_file: Path,
    reason: str,
    kind: str,
    untested: list[str],
    uncertain: list[str],
    notes: dict[str, str],
    target,
    existing_test_count: int,
    all_names: list[str] | None = None,
    function_states: dict[str, str] | None = None,
    non_public: list[str] | None = None,
) -> QualityGap:
    return QualityGap(
        source_file=source_file,
        reason=reason,
        untested_functions=untested,
        uncertain_functions=uncertain,
        coverage_notes={k: notes[k] for k in untested + uncertain if k in notes},
        existing_test_file=target.path,
        kind=kind,
        existing_test_count=existing_test_count,
        ambiguous_test_file=getattr(target, "ambiguous", False),
        test_file_alternatives=list(getattr(target, "alternatives", ()) or ()),
        all_functions=list(all_names or untested),
        function_states=dict(function_states or {}),
        non_public_functions=list(non_public or ()),
    )


def _function_states(coverage: FileCoverage) -> dict[str, str]:
    """Per-symbol coverage state for the UI: untested / uncertain / covered."""
    states: dict[str, str] = {}
    for entry in coverage.entries:
        if entry.confidence.is_covered:
            states[entry.qualified_name] = "covered"
        elif entry.confidence is CoverageConfidence.UNCERTAIN:
            states[entry.qualified_name] = "uncertain"
        else:
            states[entry.qualified_name] = "untested"
    return states
