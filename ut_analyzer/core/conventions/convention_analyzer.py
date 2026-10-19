"""Existing test convention analysis.

Extracts the project's testing style (naming, fixtures, mocks, assertions,
imports, helpers) so generated tests look like they were written by the team.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from ...models import TestConventions, TestFile
from ..discovery.naming_convention import detect_naming_convention

_IMPORT_RES = {
    "python": re.compile(r"^(?:from\s+\S+\s+import\s+.+|import\s+\S+.*)$", re.MULTILINE),
    "cpp": re.compile(r"^#include\s*[<\"].+[>\"]$", re.MULTILINE),
    "javascript": re.compile(r"^(?:import\s.+|const\s+.+=\s*require\(.+\).*)$", re.MULTILINE),
    "java": re.compile(r"^import\s+.+;$", re.MULTILINE),
    "csharp": re.compile(r"^using\s+.+;$", re.MULTILINE),
    "go": re.compile(r"^import\s+(?:\(|\").*$", re.MULTILINE),
}

# `SeekUT::SeekUT(` / `SeekUT::~SeekUT(` - a fixture body written in the test .cpp.
_CPP_FIXTURE_CTOR_RE = re.compile(r"^\s*(\w+)\s*::\s*~?\1\s*\(", re.MULTILINE)


def analyze_conventions(
    test_files: list[TestFile], language: str, test_dir: Path | None = None
) -> TestConventions:
    conventions = TestConventions()
    if not test_files:
        return conventions

    # The project's own unit-test folder is the authority on style; every
    # discovered test file is used only when that folder holds none.
    test_files = _files_in_test_dir(test_files, test_dir) or test_files

    conventions.file_naming = _file_naming_pattern(test_files)
    conventions.suite_naming = _suite_naming(test_files)
    conventions.case_naming = _case_naming(test_files)
    _detect_fixtures_and_mocks(test_files, language, conventions)
    conventions.assertion_style = _assertion_style(test_files, language)
    conventions.aaa_structure = _detect_aaa(test_files)
    conventions.common_imports = _common_imports(test_files, language)
    conventions.helper_utilities = _helper_utilities(test_files, language)
    conventions.example_test = _pick_example(test_files)
    conventions.indent = _detect_indent(test_files)
    return conventions


def _files_in_test_dir(test_files: list[TestFile], test_dir: Path | None) -> list[TestFile]:
    """Test files that live inside the project's unit-test folder."""
    if test_dir is None:
        return []
    root_key = os.path.normcase(os.path.abspath(test_dir))
    return [
        tf
        for tf in test_files
        if os.path.normcase(os.path.abspath(tf.path)).startswith(root_key + os.sep)
    ]


def _file_naming_pattern(test_files: list[TestFile]) -> str | None:
    """Dominant UT file name pattern, e.g. ``<name>UT.cpp`` or ``UT_<name>.cpp``.

    Returns ``None`` when the project mixes conventions, so callers fall back to
    the language default instead of silently committing to one of them.
    """
    convention = detect_naming_convention([tf.path for tf in test_files])
    if convention.pattern is None or convention.ambiguous:
        return None
    return convention.pattern


def _suite_naming(test_files: list[TestFile]) -> str | None:
    suites = [c.suite for tf in test_files for c in tf.cases if c.suite]
    if not suites:
        return None
    sample = suites[0]
    if re.match(r"^[A-Z][a-zA-Z0-9]*$", sample):
        prefix = "Test" if sample.startswith("Test") else ""
        return f"PascalCase{f', prefix {prefix}' if prefix else ''} (e.g. {sample})"
    return f"e.g. {sample}"


def _case_naming(test_files: list[TestFile]) -> str | None:
    names = [c.name for tf in test_files for c in tf.cases]
    if not names:
        return None
    snake = sum(1 for n in names if "_" in n and n == n.lower())
    camel = sum(1 for n in names if re.match(r"^[a-z]+[A-Z]", n))
    pascal = sum(1 for n in names if re.match(r"^[A-Z][a-zA-Z0-9]*$", n))
    style = "snake_case"
    if camel >= snake and camel >= pascal:
        style = "camelCase"
    elif pascal > snake and pascal > camel:
        style = "PascalCase"
    prefix = ""
    prefixed = sum(1 for n in names if n.lower().startswith("test"))
    if prefixed > len(names) / 2:
        prefix = ", prefixed with 'test'"
    return f"{style}{prefix} (e.g. {names[0]})"


def _detect_fixtures_and_mocks(
    test_files: list[TestFile], language: str, conventions: TestConventions
) -> None:
    joined = "\n".join(tf.content for tf in test_files[:40])

    if language == "python":
        if "@pytest.fixture" in joined:
            conventions.uses_fixtures = True
            conventions.fixture_style = "pytest @pytest.fixture functions"
        if re.search(r"\bdef setUp\(", joined):
            conventions.setup_teardown = "unittest setUp/tearDown methods"
        if re.search(r"mock\.patch|@patch\b", joined):
            conventions.mocking = "unittest.mock.patch decorators/context managers"
        elif "mocker." in joined:
            conventions.mocking = "pytest-mock 'mocker' fixture"
    elif language == "cpp":
        if "TEST_F(" in joined:
            conventions.uses_fixtures = True
            conventions.fixture_style = "GoogleTest TEST_F fixture classes"
        hooks: list[str] = []
        if re.search(r"\bvoid SetUp\(\)", joined):
            hooks.append("GoogleTest SetUp()/TearDown() overrides")
        if _CPP_FIXTURE_CTOR_RE.search(joined):
            hooks.append(
                "fixture constructor/destructor defined out-of-line in the test .cpp file"
            )
        if hooks:
            conventions.setup_teardown = ", ".join(hooks)
        if "MOCK_METHOD" in joined or re.search(r"#include\s*[<\"]gmock/", joined):
            conventions.mocking = "gMock MOCK_METHOD mock classes"
    elif language == "javascript":
        if re.search(r"\bbeforeEach\s*\(", joined):
            conventions.uses_fixtures = True
            conventions.fixture_style = "beforeEach/afterEach hooks"
            conventions.setup_teardown = "beforeEach/afterEach hooks"
        if "jest.mock(" in joined:
            conventions.mocking = "jest.mock module mocks"
        elif "vi.mock(" in joined:
            conventions.mocking = "vitest vi.mock module mocks"
        elif "sinon" in joined:
            conventions.mocking = "sinon stubs/spies"
    elif language == "java":
        if "@BeforeEach" in joined or "@Before" in joined:
            conventions.uses_fixtures = True
            conventions.fixture_style = "@BeforeEach/@AfterEach annotated methods"
            conventions.setup_teardown = "@BeforeEach/@AfterEach annotated methods"
        if "Mockito" in joined or "@Mock" in joined:
            conventions.mocking = "Mockito mocks (@Mock / Mockito.when)"
    elif language == "csharp":
        if "[SetUp]" in joined or "constructor" in joined:
            conventions.setup_teardown = "constructor / [SetUp] initialization"
        if "new Mock<" in joined:
            conventions.mocking = "Moq Mock<T> objects"
        elif "Substitute.For<" in joined:
            conventions.mocking = "NSubstitute substitutes"
    elif language == "go":
        if "t.Cleanup(" in joined or "defer " in joined:
            conventions.setup_teardown = "t.Cleanup / defer for teardown"
        if "testify" in joined:
            conventions.mocking = "testify mocks/assertions"


def _assertion_style(test_files: list[TestFile], language: str) -> str | None:
    joined = "\n".join(tf.content for tf in test_files[:40])
    checks: list[tuple[str, str]] = {
        "python": [
            (r"^\s*assert\b", "plain pytest assert statements"),
            (r"self\.assert\w+\(", "unittest self.assert* methods"),
        ],
        "cpp": [
            (r"\bEXPECT_\w+\(", "GoogleTest EXPECT_* macros"),
            (r"\bASSERT_\w+\(", "GoogleTest ASSERT_* macros"),
            (r"\bREQUIRE\(", "Catch2 REQUIRE macros"),
            (r"\bCHECK\(", "Catch2/doctest CHECK macros"),
        ],
        "javascript": [
            (r"\bexpect\(.+\)\.to\b", "chai expect(...).to style"),
            (r"\bexpect\(", "expect(...) matchers"),
            (r"\bassert\.", "node assert module"),
        ],
        "java": [
            (r"\bassertThat\(", "AssertJ/Hamcrest assertThat"),
            (r"\bassert\w+\(", "JUnit assert* methods"),
        ],
        "csharp": [
            (r"\.Should\(\)", "FluentAssertions .Should()"),
            (r"\bAssert\.\w+", "Assert.* methods"),
        ],
        "go": [
            (r"\bassert\.\w+\(", "testify assert package"),
            (r"\bt\.(Errorf?|Fatalf?)\b", "plain t.Error/t.Fatal checks"),
        ],
    }.get(language, [])
    counts = [(desc, len(re.findall(pat, joined, re.MULTILINE))) for pat, desc in checks]
    counts = [c for c in counts if c[1] > 0]
    if not counts:
        return None
    counts.sort(key=lambda c: -c[1])
    return counts[0][0]


def _detect_aaa(test_files: list[TestFile]) -> bool:
    """Heuristic: bodies with 2+ blank-line-separated blocks or AAA comments."""
    sample = [c.body for tf in test_files for c in tf.cases if c.body][:30]
    if not sample:
        return False
    hits = 0
    for body in sample:
        if re.search(r"//\s*(Arrange|Act|Assert)|#\s*(Arrange|Act|Assert)", body, re.IGNORECASE):
            hits += 1
        elif body.count("\n\n") >= 2:
            hits += 1
    return hits >= len(sample) / 2


def _common_imports(test_files: list[TestFile], language: str) -> list[str]:
    pattern = _IMPORT_RES.get(language)
    if pattern is None:
        return []
    counter: Counter[str] = Counter()
    for tf in test_files[:60]:
        for match in pattern.findall(tf.content):
            counter[match.strip()] += 1
    return [imp for imp, count in counter.most_common(8) if count >= 2] or [
        imp for imp, _ in counter.most_common(5)
    ]


def _helper_utilities(test_files: list[TestFile], language: str) -> list[str]:
    helpers: list[str] = []
    for tf in test_files:
        name = tf.path.name.lower()
        if any(k in name for k in ("helper", "util", "fixture", "common", "conftest")):
            helpers.append(tf.path.name)
    seen: set[str] = set()
    return [h for h in helpers if not (h in seen or seen.add(h))][:10]


def _pick_example(test_files: list[TestFile]) -> str:
    """Pick a representative, medium-sized existing test as a style example."""
    candidates = [c for tf in test_files for c in tf.cases if c.body and c.assertion_count > 0]
    if not candidates:
        return ""
    candidates.sort(key=lambda c: abs(len(c.body) - 600))
    return candidates[0].body[:2000]


def _detect_indent(test_files: list[TestFile]) -> str:
    for tf in test_files[:10]:
        for line in tf.content.splitlines():
            stripped = line.lstrip()
            if stripped and line != stripped:
                whitespace = line[: len(line) - len(stripped)]
                if whitespace.startswith("\t"):
                    return "\t"
                if len(whitespace) in (2, 4):
                    return " " * len(whitespace)
    return "    "
