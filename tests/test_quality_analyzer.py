"""Tests for quality gap detection."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.adapters.python.adapter import PythonAdapter
from ut_analyzer.core.quality.quality_analyzer import analyze_quality
from ut_analyzer.models import (
    GAP_ADDITIONAL_CASES,
    GAP_NO_TEST_FILE,
    GAP_UNTESTED_FUNCTIONS,
    EnvironmentInfo,
    ProjectConfig,
    ScanResult,
    ScannedFile,
    TestCase,
    TestFile,
)


def _make_scan(root: Path, sources: list[Path]) -> ScanResult:
    return ScanResult(
        root=root,
        files=[
            ScannedFile(path=p, language="python", is_test=False)
            for p in sources
        ],
    )


def test_gap_when_one_function_untested(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    source = src_dir / "calc.py"
    source.write_text(
        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    test_file = tests_dir / "test_calc.py"
    test_file.write_text(
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    test_files = [
        TestFile(
            path=test_file,
            language="python",
            framework="pytest",
            content=test_file.read_text(encoding="utf-8"),
        )
    ]
    config = ProjectConfig(root=tmp_path, test_dir=tests_dir)
    env = EnvironmentInfo(language="python", test_framework="pytest")
    scan = _make_scan(tmp_path, [source])

    report = analyze_quality(scan, test_files, adapter, env, config)

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.source_file == source
    assert "sub" in gap.untested_functions
    assert gap.existing_test_file == test_file
    assert gap.kind == GAP_UNTESTED_FUNCTIONS
    assert gap.has_existing_tests


def test_gap_sets_planned_ut_path_when_no_test_file(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    source = src_dir / "util.py"
    source.write_text("def helper():\n    return 1\n", encoding="utf-8")

    config = ProjectConfig(root=tmp_path, test_dir=tests_dir)
    env = EnvironmentInfo(language="python", test_framework="pytest")
    scan = _make_scan(tmp_path, [source])

    report = analyze_quality(scan, [], adapter, env, config)

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.existing_test_file == tests_dir / "test_util.py"
    assert gap.reason == "no test file covers this source file"
    assert gap.kind == GAP_NO_TEST_FILE
    assert not gap.has_existing_tests


def test_fully_tested_file_is_listed_for_additional_cases(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file = tests_dir / "test_calc.py"
    test_file.write_text(
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    test_files = [
        TestFile(
            path=test_file,
            language="python",
            framework="pytest",
            cases=[TestCase(name="test_add", file=test_file, assertion_count=1)],
            content=test_file.read_text(encoding="utf-8"),
        )
    ]
    config = ProjectConfig(root=tmp_path, test_dir=tests_dir)
    env = EnvironmentInfo(language="python", test_framework="pytest")
    scan = _make_scan(tmp_path, [source])

    report = analyze_quality(scan, test_files, adapter, env, config)

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.kind == GAP_ADDITIONAL_CASES
    assert gap.has_existing_tests
    assert gap.untested_functions == []
    assert gap.existing_test_file == test_file
    assert gap.existing_test_count == 1
    assert report.files_without_tests == []
    assert len(report.files_with_tests) == 1
    # A covered file still offers its functions for an explicit selection.
    assert gap.selectable_functions() == ["add"]
    assert gap.coverage_state("add") == "covered"
    assert gap.default_selection() == ["add"]


def test_gap_lists_private_functions_for_selection(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    source = src_dir / "calc.py"
    source.write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def _normalize(v):\n"
        "    return v or 0\n"
        "\n"
        "class Calc:\n"
        "    def total(self):\n"
        "        return 0\n"
        "\n"
        "    def _cache(self):\n"
        "        return {}\n",
        encoding="utf-8",
    )

    config = ProjectConfig(root=tmp_path, test_dir=tests_dir)
    env = EnvironmentInfo(language="python", test_framework="pytest")
    scan = _make_scan(tmp_path, [source])

    report = analyze_quality(scan, [], adapter, env, config)

    gap = report.gaps[0]
    names = gap.selectable_functions()
    assert {"add", "_normalize", "Calc.total", "Calc._cache"} <= set(names)
    assert gap.is_non_public("_normalize")
    assert gap.is_non_public("Calc._cache")
    assert not gap.is_non_public("add")
    # Non-public members are listed but do not change the reported gap reason.
    assert "_normalize" not in gap.untested_functions


def test_selection_focuses_generation(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    source = src_dir / "calc.py"
    source.write_text(
        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    config = ProjectConfig(root=tmp_path)
    env = EnvironmentInfo(language="python", test_framework="pytest")

    report = analyze_quality(_make_scan(tmp_path, [source]), [], adapter, env, config)
    gap = report.gaps[0]

    focused = gap.focused_on(["sub"])

    assert focused.untested_functions == ["sub"]
    assert focused.focus_only
    assert gap.untested_functions != ["sub"]  # the original gap is untouched


def _cpp_scan(root: Path, sources: list[Path]) -> ScanResult:
    return ScanResult(
        root=root,
        files=[ScannedFile(path=p, language="cpp", is_test=False) for p in sources],
    )


def test_cpp_gap_lists_declarations_from_header_in_another_folder(
    tmp_path: Path,
) -> None:
    adapter = CppAdapter()
    include_dir = tmp_path / "include"
    src_dir = tmp_path / "src"
    include_dir.mkdir()
    src_dir.mkdir()

    header = include_dir / "engine.h"
    header.write_text(
        "class Engine {\n"
        "public:\n"
        "    virtual void start();\n"
        "    virtual bool isReady() const;\n"
        "    int count() const override;\n"
        "    static bool helper(int a);\n"
        "protected:\n"
        "    virtual void hook();\n"
        "private:\n"
        "    bool validate(int x) const;\n"
        "};\n"
        "void freeHelper(int a);\n",
        encoding="utf-8",
    )
    source = src_dir / "engine.cpp"
    source.write_text(
        '#include "engine.h"\n'
        "void Engine::start() {}\n"
        "bool Engine::isReady() const { return true; }\n",
        encoding="utf-8",
    )

    config = ProjectConfig(root=tmp_path)
    env = EnvironmentInfo(language="cpp")
    report = analyze_quality(
        _cpp_scan(tmp_path, [source, header]), [], adapter, env, config
    )

    gaps = {g.source_file.name: g for g in report.gaps}
    assert "engine.h" not in gaps  # the header is folded into its .cpp
    names = set(gaps["engine.cpp"].selectable_functions())
    assert {
        "Engine::start",
        "Engine::isReady",
        "Engine::count",
        "Engine::helper",
        "Engine::hook",
        "Engine::validate",
        "freeHelper",
    } <= names


def test_cpp_gap_lists_only_the_functions_declared_in_the_header(
    tmp_path: Path,
) -> None:
    """Picking ``x.cpp`` must offer exactly what ``x.h`` declares."""
    adapter = CppAdapter()
    header = tmp_path / "engine.h"
    header.write_text(
        "class Engine {\n"
        "public:\n"
        "    void start();\n"
        "    bool isReady() const;\n"
        "};\n",
        encoding="utf-8",
    )
    source = tmp_path / "engine.cpp"
    source.write_text(
        '#include "engine.h"\n'
        "static int localHelper(int a) { return a; }\n"
        "void Engine::start() {}\n"
        "bool Engine::isReady() const { return true; }\n"
        "void notDeclaredAnywhere() {}\n",
        encoding="utf-8",
    )

    config = ProjectConfig(root=tmp_path)
    env = EnvironmentInfo(language="cpp")
    report = analyze_quality(
        _cpp_scan(tmp_path, [source, header]), [], adapter, env, config
    )

    gap = next(g for g in report.gaps if g.source_file == source)
    assert set(gap.selectable_functions()) == {"Engine::start", "Engine::isReady"}


def test_cpp_gap_keeps_cpp_only_functions_when_there_is_no_header(
    tmp_path: Path,
) -> None:
    adapter = CppAdapter()
    source = tmp_path / "tools.cpp"
    source.write_text("int addTwo(int a) { return a + 2; }\n", encoding="utf-8")

    config = ProjectConfig(root=tmp_path)
    env = EnvironmentInfo(language="cpp")
    report = analyze_quality(_cpp_scan(tmp_path, [source]), [], adapter, env, config)

    gap = next(g for g in report.gaps if g.source_file == source)
    assert gap.selectable_functions() == ["addTwo"]


def test_cpp_header_without_source_file_is_analyzed(tmp_path: Path) -> None:
    adapter = CppAdapter()
    header = tmp_path / "tiny.h"
    header.write_text(
        "class Tiny {\n"
        "public:\n"
        "    virtual bool ping() const { return true; }\n"
        "    void reset() { n_ = 0; }\n"
        "private:\n"
        "    int n_ = 0;\n"
        "};\n",
        encoding="utf-8",
    )

    config = ProjectConfig(root=tmp_path)
    env = EnvironmentInfo(language="cpp")
    report = analyze_quality(_cpp_scan(tmp_path, [header]), [], adapter, env, config)

    gap = report.gaps[0]
    assert gap.source_file == header
    assert set(gap.selectable_functions()) == {"Tiny::ping", "Tiny::reset"}
