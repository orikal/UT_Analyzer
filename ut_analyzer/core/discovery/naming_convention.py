"""Test-file naming convention detection with explicit ambiguity reporting.

Picking the single most common pattern silently is wrong when a project mixes
conventions (``FooUT.cpp`` next to ``foo_test.cpp``): the tool would keep
writing into the wrong file. This module keeps every candidate and says when
the evidence is not decisive.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: Markers that turn a source-file name into a test-file name, longest first so
#: "unittest" is not truncated to "test".
TEST_STEM_MARKERS: tuple[str, ...] = (
    "unittests",
    "unittest",
    "tests",
    "test",
    "specs",
    "spec",
    "uts",
    "ut",
)
_STEM_SEPARATORS = "_-."


def split_test_stem(test_stem: str) -> tuple[str, str, str] | None:
    """Split a test-file stem into ``(prefix, source_stem, suffix)``.

    The affixes keep their original spelling and separator, so the project's
    convention can be reproduced verbatim (``SeekUT`` -> ``("", "Seek", "UT")``,
    ``UT_seek`` -> ``("UT_", "seek", "")``, ``test_calc`` -> ``("test_", "calc", "")``).
    Returns None when the stem carries no recognizable test marker.
    """
    for marker in TEST_STEM_MARKERS:
        size = len(marker)
        if len(test_stem) <= size:
            continue
        if test_stem[-size:].lower() == marker:
            head = test_stem[:-size]
            separator = ""
            if head[-1] in _STEM_SEPARATORS:
                separator, head = head[-1], head[:-1]
            if head:
                return "", head, f"{separator}{test_stem[-size:]}"
        if test_stem[:size].lower() == marker:
            tail = test_stem[size:]
            separator = ""
            if tail[0] in _STEM_SEPARATORS:
                separator, tail = tail[0], tail[1:]
            if tail:
                return f"{test_stem[:size]}{separator}", tail, ""
    return None


def source_stem_from_test_stem(test_stem: str) -> str | None:
    """Extract the source-file stem implied by a test file stem, or None."""
    parts = split_test_stem(test_stem)
    return parts[1].lower() if parts is not None else None

#: Share of classified test files the winner must reach to be decisive.
DOMINANT_SHARE = 0.6

#: Below this many classified test files a single example proves little.
MIN_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class NamingCandidate:
    """One observed naming pattern and the files that support it."""

    pattern: str
    count: int
    examples: tuple[str, ...] = ()

    def describe(self) -> str:
        sample = f" (e.g. {self.examples[0]})" if self.examples else ""
        return f"{self.pattern} x{self.count}{sample}"


@dataclass(frozen=True, slots=True)
class NamingConvention:
    """What the project's test files say about how new ones should be named."""

    pattern: str | None = None
    candidates: tuple[NamingCandidate, ...] = ()
    classified: int = 0
    unclassified: int = 0
    from_default: bool = False

    @property
    def ambiguous(self) -> bool:
        """True when more than one convention is in real use."""
        if len(self.candidates) < 2 or not self.classified:
            return False
        return self.candidates[0].count / self.classified < DOMINANT_SHARE

    @property
    def confidence(self) -> float:
        if self.from_default or not self.classified or not self.candidates:
            return 0.0
        return round(self.candidates[0].count / self.classified, 2)

    @property
    def alternatives(self) -> tuple[str, ...]:
        return tuple(c.pattern for c in self.candidates[1:])

    def describe(self) -> str:
        if self.from_default or self.pattern is None:
            return "no naming convention could be derived from existing test files"
        if self.ambiguous:
            competing = ", ".join(c.describe() for c in self.candidates[:3])
            return f"mixed naming conventions in use: {competing}"
        return f"{self.pattern} (used by {self.candidates[0].count} of {self.classified} test files)"


def detect_naming_convention(test_paths: Sequence[Path]) -> NamingConvention:
    """Derive the naming convention from real test file names."""
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    unclassified = 0

    for path in test_paths:
        parts = split_test_stem(path.stem)
        if parts is None:
            unclassified += 1
            continue
        prefix, _name, suffix = parts
        pattern = f"{prefix}<name>{suffix}{path.suffix}"
        counts[pattern] += 1
        examples.setdefault(pattern, []).append(path.name)

    if not counts:
        return NamingConvention(unclassified=unclassified, from_default=True)

    candidates = tuple(
        NamingCandidate(pattern, count, tuple(examples[pattern][:3]))
        for pattern, count in counts.most_common()
    )
    classified = sum(counts.values())
    convention = NamingConvention(
        pattern=candidates[0].pattern,
        candidates=candidates,
        classified=classified,
        unclassified=unclassified,
    )
    if classified < MIN_SAMPLES and len(candidates) == 1:
        # A single example is a weak signal, but still better than nothing.
        return convention
    return convention


def apply_pattern(pattern: str, source: Path) -> str:
    """Render ``pattern`` for a source file (``<name>UT.cpp`` -> ``SeekUT.cpp``)."""
    return pattern.replace("<name>", source.stem)
