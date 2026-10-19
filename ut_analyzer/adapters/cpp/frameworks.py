"""C++ test framework detection and test-case parsing (GoogleTest, Catch2, doctest)."""

from __future__ import annotations

import re
from pathlib import Path

from ...models import TestCase

# Strong signals: framework-specific #include lines (primary evidence).
_INCLUDE_MARKERS: dict[str, re.Pattern[str]] = {
    "GoogleTest": re.compile(
        r'#include\s*[<"](?:gtest/gtest\.h|gmock/gmock\.h|gtest\.h)[>"]',
        re.IGNORECASE,
    ),
    "Catch2": re.compile(
        r'#include\s*[<"]catch2/',
        re.IGNORECASE,
    ),
    "doctest": re.compile(
        r'#include\s*[<"]doctest(?:\.h|/)',
        re.IGNORECASE,
    ),
    "Boost.Test": re.compile(
        r'#include\s*[<"]boost/test/',
        re.IGNORECASE,
    ),
}

# Secondary signals: macros/assertions that belong to one framework only.
_MACRO_MARKERS: dict[str, re.Pattern[str]] = {
    "GoogleTest": re.compile(
        r"\bTEST(_F|_P|TYPED_TEST|P)\s*\(\s*\w+\s*,\s*\w+\s*[,)]"
        r"|\b(EXPECT_|ASSERT_)\w+\s*\(",
        re.MULTILINE,
    ),
    "Catch2": re.compile(
        r"\b(SCENARIO|SECTION|GIVEN|WHEN|THEN)\s*\(\s*\"",
        re.MULTILINE,
    ),
    "doctest": re.compile(
        r"\bDOCTEST_(ASSERT|CHECK|REQUIRE)\w*\s*\(",
        re.MULTILINE,
    ),
    "Boost.Test": re.compile(
        r"\bBOOST_(AUTO_TEST_CASE|FIXTURE_TEST_CASE|TEST)\s*\(",
        re.MULTILINE,
    ),
}

MOCK_MARKERS = {
    "gMock": r"#include\s*[<\"]gmock/gmock\.h[>\"]|\bMOCK_METHOD",
    "FakeIt": r"#include\s*[<\"]fakeit",
    "trompeloeil": r"#include\s*[<\"]trompeloeil",
}

_TEST_MACRO_RE = re.compile(
    r"\b(TEST|TEST_F|TEST_P|TYPED_TEST)\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)", re.MULTILINE
)
_CATCH_CASE_RE = re.compile(
    r"\b(TEST_CASE|SCENARIO)\s*\(\s*\"([^\"]+)\"", re.MULTILINE
)
_BOOST_CASE_RE = re.compile(r"\bBOOST_AUTO_TEST_CASE\s*\(\s*(\w+)\s*\)", re.MULTILINE)

ASSERTION_RE = re.compile(
    r"\b(EXPECT_\w+|ASSERT_\w+|REQUIRE(_\w+)?|CHECK(_\w+)?|BOOST_(CHECK|REQUIRE|TEST)\w*)\s*\("
)


def detect_framework(test_contents: list[str]) -> str | None:
    """Detect the C++ test framework by analyzing existing unit-test source files."""
    if not test_contents:
        return None

    scores: dict[str, int] = {}
    for content in test_contents:
        for name, pattern in _INCLUDE_MARKERS.items():
            if pattern.search(content):
                scores[name] = scores.get(name, 0) + 10
        for name, pattern in _MACRO_MARKERS.items():
            if pattern.search(content):
                scores[name] = scores.get(name, 0) + 3

    if not scores:
        return None

    best_score = max(scores.values())
    winners = [name for name, score in scores.items() if score == best_score]
    # Prefer GoogleTest when tied — most common in legacy C++ codebases.
    for preferred in ("GoogleTest", "Catch2", "doctest", "Boost.Test"):
        if preferred in winners:
            return preferred
    return winners[0]


def detect_mocking(test_contents: list[str]) -> list[str]:
    found: list[str] = []
    for name, marker in MOCK_MARKERS.items():
        if any(re.search(marker, c) for c in test_contents):
            found.append(name)
    return found


def extract_brace_block(content: str, open_index: int) -> str:
    """Return the ``{...}`` block starting at ``open_index`` (index of '{')."""
    depth = 0
    for i in range(open_index, len(content)):
        ch = content[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[open_index : i + 1]
    return content[open_index:]


def parse_cases(path: Path, content: str) -> list[TestCase]:
    cases: list[TestCase] = []

    for match in _TEST_MACRO_RE.finditer(content):
        suite, name = match.group(2), match.group(3)
        cases.append(_make_case(path, content, match.end(), name, suite))

    for match in _CATCH_CASE_RE.finditer(content):
        cases.append(_make_case(path, content, match.end(), match.group(2), None))

    for match in _BOOST_CASE_RE.finditer(content):
        cases.append(_make_case(path, content, match.end(), match.group(1), None))

    return cases


def _make_case(
    path: Path, content: str, search_from: int, name: str, suite: str | None
) -> TestCase:
    brace = content.find("{", search_from)
    body = extract_brace_block(content, brace) if brace != -1 else ""
    line = content.count("\n", 0, search_from) + 1
    return TestCase(
        name=name,
        suite=suite,
        file=path,
        line=line,
        body=body,
        assertion_count=len(ASSERTION_RE.findall(body)),
    )
