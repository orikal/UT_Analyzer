"""Tests for LLM prompt building and response parsing."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.core.generation.prompt_builder import (
    TEST_END,
    TEST_START,
    build_file_map,
    build_generation_prompt,
    ensure_english_test_comments,
    parse_generation_response,
)
from ut_analyzer.models import (
    GAP_ADDITIONAL_CASES,
    EnvironmentInfo,
    QualityGap,
    TestConventions,
)


def _minimal_gap() -> QualityGap:
    return QualityGap(
        source_file=__import__("pathlib").Path("calc.py"),
        reason="no test file covers this source file",
        untested_functions=["add"],
    )


def test_build_generation_prompt_requires_english_comments() -> None:
    prompt = build_generation_prompt(
        source_code="def add(a, b):\n    return a + b\n",
        source_name="calc.py",
        gap=_minimal_gap(),
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        existing_test_content="",
        max_tests=2,
    )
    assert "explenation :" in prompt
    assert "Input Params :" in prompt
    assert "Expected Result :" in prompt


def test_build_file_map_lists_every_real_path() -> None:
    root = Path("/repo")
    file_map = build_file_map(
        root / "src" / "calc.py",
        root=root,
        target_test_file=root / "tests" / "test_calc.py",
        target_is_new=True,
        related_files=[root / "src" / "calc.pyi"],
        dependency_files=[root / "src" / "math_utils.py", root / "src" / "math_utils.py"],
        test_dir=root / "tests",
        knowledge_files=[root / ".ut_analyzer" / "INDEX.md"],
    )
    assert str(root / "src" / "calc.py") in file_map
    assert str(root / "src" / "calc.pyi") in file_map
    assert str(root / "tests" / "test_calc.py") in file_map
    assert "does not exist yet" in file_map
    assert str(root / "src" / "math_utils.py") in file_map
    # Duplicated dependencies are listed once.
    assert file_map.count(str(root / "src" / "math_utils.py")) == 1
    assert str(root / ".ut_analyzer" / "INDEX.md") in file_map
    assert str(root) in file_map


def test_build_file_map_respects_the_dependency_limit() -> None:
    file_map = build_file_map(
        Path("/repo/src/calc.py"),
        dependency_files=[Path(f"/repo/src/dep{index}.py") for index in range(10)],
        max_dependencies=3,
    )
    assert "dep2.py" in file_map
    assert "dep3.py" not in file_map


def test_generation_prompt_keeps_the_file_map_when_the_budget_is_tiny() -> None:
    file_map = build_file_map(Path("/repo/src/calc.py"), root=Path("/repo"))
    prompt = build_generation_prompt(
        source_code="def add(a, b):\n    return a + b\n",
        source_name="calc.py",
        gap=_minimal_gap(),
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        existing_test_content="",
        max_tests=2,
        file_map=file_map,
        repo_context="x" * 5000,
        max_total_chars=1,
    )
    assert "Files for this task" in prompt
    assert str(Path("/repo/src/calc.py")) in prompt
    assert "x" * 5000 not in prompt


def test_generation_prompt_without_file_map_skips_the_rule() -> None:
    prompt = build_generation_prompt(
        source_code="def add(a, b):\n    return a + b\n",
        source_name="calc.py",
        gap=_minimal_gap(),
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        existing_test_content="",
        max_tests=2,
    )
    assert "Files for this task" not in prompt
    assert "ABOVE each test function header" in prompt


def test_build_generation_prompt_asks_for_additional_cases() -> None:
    gap = QualityGap(
        source_file=__import__("pathlib").Path("calc.py"),
        reason="this file already has unit tests",
        untested_functions=[],
        kind=GAP_ADDITIONAL_CASES,
        existing_test_count=2,
    )
    prompt = build_generation_prompt(
        source_code="def add(a, b):\n    return a + b\n",
        source_name="calc.py",
        gap=gap,
        env=EnvironmentInfo(language="python", test_framework="pytest"),
        conventions=TestConventions(),
        existing_test_content="def test_add():\n    assert add(1, 2) == 3\n",
        max_tests=3,
    )
    assert "ADDITIONAL test cases" in prompt
    assert "do NOT duplicate any of these tests" in prompt
    assert "Focus on these untested functions" not in prompt


def test_parse_response_injects_comments_above_function_header() -> None:
    response = (
        f"{TEST_START} test_add | no | Verifies sum of integers | a=1, b=2 | 3\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        f"{TEST_END}"
    )
    parsed = parse_generation_response(response, language="python")
    assert len(parsed) == 1
    _name, code, _edge, rationale = parsed[0]
    assert rationale == "Verifies sum of integers"
    lines = code.splitlines()
    def_idx = next(i for i, line in enumerate(lines) if line.startswith("def test_add"))
    assert lines[def_idx - 3] == "# explenation : Verifies sum of integers"
    assert lines[def_idx - 2] == "# Input Params : a=1, b=2"
    assert lines[def_idx - 1] == "# Expected Result : 3"


def test_parse_response_keeps_existing_comments_above_header() -> None:
    response = (
        f"{TEST_START} test_add | no | Verifies sum | a=1, b=2 | 3\n"
        "# explenation : Verifies that add works.\n"
        "# Input Params : a=1, b=2\n"
        "# Expected Result : 3\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        f"{TEST_END}"
    )
    parsed = parse_generation_response(response, language="python")
    code = parsed[0][1]
    assert code.count("Input Params :") == 1
    assert code.count("Expected Result :") == 1


def test_ensure_comments_uses_line_comments_for_cpp() -> None:
    code = "TEST(AddTest, PositiveNumbers) {\n    EXPECT_EQ(add(1, 2), 3);\n}"
    updated = ensure_english_test_comments(
        code,
        description="Verifies addition.",
        input_params="a=1, b=2",
        expected_output="3",
        language="cpp",
    )
    lines = updated.splitlines()
    test_idx = next(i for i, line in enumerate(lines) if line.startswith("TEST("))
    assert lines[test_idx - 3] == "// explenation : Verifies addition."
    assert lines[test_idx - 2] == "// Input Params : a=1, b=2"
    assert lines[test_idx - 1] == "// Expected Result : 3"


def test_parse_response_tolerates_a_missing_end_marker() -> None:
    response = (
        f"{TEST_START} test_add | no | sums | a=1, b=2 | 3\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "\n"
        f"{TEST_START} test_sub | yes | subtracts | a=1, b=2 | -1\n"
        "def test_sub():\n"
        "    assert sub(1, 2) == -1\n"
    )
    parsed = parse_generation_response(response, language="python")
    assert [name for name, _code, _edge, _rationale in parsed] == [
        "test_add",
        "test_sub",
    ]
    assert parsed[1][2] is True


def test_parse_response_reads_raw_cpp_tests_without_markers() -> None:
    response = (
        'Here are the tests:\n\n#include "calcUT.h"\n\n'
        "// explenation : adds two numbers\n"
        "TEST_F(CalcUT, AddsTwoNumbers)\n"
        "{\n"
        '    EXPECT_EQ(calc.Add(1, 2), 3); // "{" inside a comment\n'
        "}\n\n"
        "TEST_F(CalcUT, HandlesZero)\n"
        "{\n"
        "    EXPECT_EQ(calc.Add(0, 0), 0);\n"
        "}\n"
    )
    parsed = parse_generation_response(response, language="cpp")
    assert [name for name, _code, _edge, _rationale in parsed] == [
        "AddsTwoNumbers",
        "HandlesZero",
    ]
    assert parsed[0][1].startswith("// explenation : adds two numbers")
    assert "#include" not in parsed[0][1]


def test_parse_response_reads_markdown_headings_with_fences() -> None:
    response = (
        "### Test 1: add returns the sum\n"
        "```cpp\n"
        "TEST(AddTest, Positive) {\n"
        "    EXPECT_EQ(add(1, 2), 3);\n"
        "}\n"
        "```\n"
    )
    parsed = parse_generation_response(response, language="cpp")
    assert len(parsed) == 1
    assert parsed[0][0] == "Positive"
    assert "EXPECT_EQ" in parsed[0][1]


def test_parse_response_salvages_a_reply_cut_off_mid_test() -> None:
    response = (
        "TEST_F(CalcUT, AddsTwoNumbers)\n"
        "{\n"
        "    EXPECT_EQ(calc.Add(1, 2), 3);\n"
    )
    parsed = parse_generation_response(response, language="cpp")
    assert len(parsed) == 1
    code = parsed[0][1]
    assert "TODO" in code
    assert code.rstrip().endswith("}")


def test_parse_response_ignores_the_thinking_channel() -> None:
    response = (
        "<think>Let me plan the test for add.</think>\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
    )
    parsed = parse_generation_response(response, language="python")
    assert len(parsed) == 1
    assert "Let me plan" not in parsed[0][1]


def test_parse_response_returns_nothing_for_prose() -> None:
    response = "I am sorry, but I cannot write tests for this file."
    assert parse_generation_response(response, language="cpp") == []
