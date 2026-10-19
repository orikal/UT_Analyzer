"""Test-file structure learned from the project's own tests.

The shape of a new test file - which headers/imports it opens with, what its
fixture derives from, which case macro it uses - is read off real neighbouring
test files instead of being hard-coded per language. A tool that assumes
``TestEnvironment.h`` exists is wrong in every repository that does not have it.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ...models import TestFile
from ...models.semantics import FileSemantics, SymbolKind
from ...models.tests import FixtureLifecycle
from ...utils.fs import read_text_safe
from ..semantics import extract_file
from .fixture_lifecycle import analyze_fixture_lifecycle

#: How many neighbouring test files to learn from.
MAX_NEIGHBOURS = 12

#: Share of neighbours that must share an import before it counts as boilerplate.
SHARED_IMPORT_SHARE = 0.5

#: Extensions that mark an import target as a file name rather than a dotted path.
_FILE_SUFFIXES = {
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inl",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".cs",
    ".go",
}


@dataclass(frozen=True, slots=True)
class TestTemplate:
    """Concrete structural facts observed in nearby test files."""

    #: Import/include lines that nearly every neighbouring test file opens with.
    preamble: tuple[str, ...] = ()
    #: What neighbouring fixtures derive from, e.g. ``::testing::Test``.
    fixture_base: str | None = None
    #: The macro or decorator that declares a test case, e.g. ``TEST_F``.
    case_macro: str | None = None
    #: A full nearby test case, used as the shape to imitate.
    example: str = ""
    #: Where the example came from, so the prompt can cite it.
    example_file: Path | None = None
    #: Test files the template was derived from.
    sources: tuple[Path, ...] = ()
    #: Which lifecycle hooks the neighbouring fixtures use, and where they live.
    lifecycle: FixtureLifecycle | None = None

    @property
    def is_empty(self) -> bool:
        return not self.preamble and not self.example and self.case_macro is None

    def preamble_text(self) -> str:
        return "".join(f"{line}\n" for line in self.preamble)

    def describe(self) -> str:
        if self.is_empty:
            return "no neighbouring test files were available to learn from"
        parts = [f"learned from {len(self.sources)} nearby test file(s)"]
        if self.case_macro:
            parts.append(f"cases declared with {self.case_macro}")
        if self.fixture_base:
            parts.append(f"fixtures derive from {self.fixture_base}")
        if self.lifecycle is not None and not self.lifecycle.is_empty:
            parts.append(self.lifecycle.describe())
        return "; ".join(parts)


def derive_test_template(
    target_test_path: Path,
    source: Path,
    test_files: Sequence[TestFile],
    language: str,
) -> TestTemplate:
    """Learn the structure of new tests from the closest existing test files."""
    neighbours = _closest(target_test_path, test_files)
    if not neighbours:
        return TestTemplate()

    parsed = [extract_file(tf.path, language, tf.content) for tf in neighbours]
    return TestTemplate(
        preamble=_shared_imports(parsed, source),
        fixture_base=_dominant_fixture_base(parsed),
        case_macro=_dominant_case_macro(parsed),
        example=_example_case(parsed),
        example_file=next(
            (s.path for s in parsed if s.tests()), None
        ),
        sources=tuple(tf.path for tf in neighbours),
        lifecycle=analyze_fixture_lifecycle(
            _lifecycle_sources(neighbours), language
        ),
    )


def _lifecycle_sources(neighbours: Sequence[TestFile]) -> list[tuple[str, str]]:
    """Neighbouring test files plus their companion headers.

    A C++ fixture is declared in the ``.h`` and usually defined in the ``.cpp``;
    both halves are needed to see the whole lifecycle.
    """
    sources: list[tuple[str, str]] = []
    for tf in neighbours:
        if tf.content:
            sources.append((tf.path.name, tf.content))
        for extension in (".h", ".hpp", ".hxx"):
            header = tf.path.with_suffix(extension)
            if header == tf.path or not header.is_file():
                continue
            content = read_text_safe(header)
            if content:
                sources.append((header.name, content))
            break
    return sources


def _closest(target: Path, test_files: Sequence[TestFile]) -> list[TestFile]:
    """Test files ranked by directory proximity to where the new file will go."""
    target_dir = os.path.normcase(str(target.parent))
    scored: list[tuple[int, TestFile]] = []
    for tf in test_files:
        if tf.path == target:
            continue
        candidate_dir = os.path.normcase(str(tf.path.parent))
        if candidate_dir == target_dir:
            distance = 0
        elif candidate_dir.startswith(target_dir) or target_dir.startswith(candidate_dir):
            distance = 1
        else:
            distance = 2
        scored.append((distance, tf))
    scored.sort(key=lambda item: (item[0], str(item[1].path)))
    return [tf for _, tf in scored[:MAX_NEIGHBOURS]]


def _shared_imports(parsed: Sequence[FileSemantics], source: Path) -> tuple[str, ...]:
    """Imports common to the neighbours, minus each file's own subject.

    A neighbour's ``#include "Seek.h"`` is about *its* subject, not ours, so
    imports whose stem matches the neighbour's own name are dropped.
    """
    counts: Counter[str] = Counter()
    order: list[str] = []
    for semantics in parsed:
        own_stems = _subject_stems(semantics.path)
        seen: set[str] = set()
        for declaration in semantics.imports:
            raw = declaration.raw.strip()
            stem = _module_stem(declaration.module)
            if not raw or stem in own_stems or raw in seen:
                continue
            seen.add(raw)
            if raw not in counts:
                order.append(raw)
            counts[raw] += 1

    if not counts:
        return ()
    threshold = max(1, int(len(parsed) * SHARED_IMPORT_SHARE))
    shared = [line for line in order if counts[line] >= threshold]
    source_stem = source.stem.lower()
    return tuple(line for line in shared if source_stem not in line.lower())


def _module_stem(module: str) -> str:
    """The bare name of an imported module or included file.

    ``alpha.h`` is a file name, ``package.alpha`` is a dotted module path. Both
    have to end up as ``alpha``, so dots are only treated as separators when the
    trailing part is not a known source-file extension.
    """
    name = module.strip()
    if not name:
        return ""
    if Path(name).suffix.lower() not in _FILE_SUFFIXES:
        name = name.replace(".", "/")
    return Path(name).stem.lower()


def _subject_stems(path: Path | None) -> set[str]:
    if path is None:
        return set()
    stem = path.stem.lower()
    stems = {stem}
    for marker in ("_test", "test_", "ut", "_spec", "spec_", "tests"):
        stems.add(stem.replace(marker, ""))
    return {s for s in stems if s}


def _dominant_fixture_base(parsed: Sequence[FileSemantics]) -> str | None:
    bases: Counter[str] = Counter()
    for semantics in parsed:
        for symbol in semantics.symbols:
            if symbol.kind is SymbolKind.FIXTURE:
                for base in symbol.base_types:
                    bases[base] += 1
    return bases.most_common(1)[0][0] if bases else None


def _dominant_case_macro(parsed: Sequence[FileSemantics]) -> str | None:
    macros: Counter[str] = Counter()
    for semantics in parsed:
        for test in semantics.tests():
            for decorator in test.decorators:
                macros[decorator] += 1
    return macros.most_common(1)[0][0] if macros else None


def _example_case(parsed: Sequence[FileSemantics]) -> str:
    """The shortest complete nearby test - long ones crowd out real evidence."""
    bodies = [
        (len(test.body), test.body)
        for semantics in parsed
        for test in semantics.tests()
        if test.body.strip()
    ]
    if not bodies:
        return ""
    bodies.sort()
    return bodies[0][1]
