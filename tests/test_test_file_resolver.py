"""Tests for canonical unit-test file resolution."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.adapters.python.adapter import PythonAdapter
from ut_analyzer.core.discovery.test_file_resolver import (
    find_matching_test_file,
    resolve_target_test_file,
    source_stem_from_test_stem,
)
from ut_analyzer.models import TestConventions, TestFile


def test_source_stem_from_test_prefix() -> None:
    assert source_stem_from_test_stem("test_foo") == "foo"


def test_source_stem_from_test_suffix() -> None:
    assert source_stem_from_test_stem("foo_test") == "foo"


def test_find_matching_test_file_prefers_exact_stem() -> None:
    source = Path("/proj/src/calc.py")
    tests = [
        TestFile(path=Path("/proj/tests/test_other.py"), language="python", content=""),
        TestFile(path=Path("/proj/tests/test_calc.py"), language="python", content="def test_add(): pass"),
    ]
    matched = find_matching_test_file(source, tests)
    assert matched is not None
    assert matched.path.name == "test_calc.py"


def test_resolve_existing_discovered_test_file() -> None:
    adapter = PythonAdapter()
    source = Path("/proj/src/calc.py")
    test_files = [
        TestFile(
            path=Path("/proj/tests/test_calc.py"),
            language="python",
            framework="pytest",
            content="def test_add(): pass",
        )
    ]
    root = Path("/proj")
    tests_dir = Path("/proj/tests")
    target = resolve_target_test_file(
        source, adapter, TestConventions(), test_files, tests_dir, root
    )
    assert target.path == Path("/proj/tests/test_calc.py")
    assert target.is_new_file is False
    assert target.framework == "pytest"


def test_resolve_canonical_new_path_when_no_test_exists(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    target = resolve_target_test_file(
        source,
        adapter,
        TestConventions(file_naming="test_<name>.py"),
        [],
        tests_dir,
        tmp_path,
    )
    assert target.path == tests_dir / "test_calc.py"
    assert target.is_new_file is True
    assert target.content == ""


def test_resolve_new_path_follows_ut_naming_convention(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    target = resolve_target_test_file(
        source,
        adapter,
        TestConventions(file_naming="<name>UT.py"),
        [],
        tests_dir,
        tmp_path,
    )
    assert target.path == tests_dir / "calcUT.py"
    assert target.is_new_file is True


def test_resolve_new_path_falls_back_to_language_default_without_convention(
    tmp_path: Path,
) -> None:
    """With no test files to learn from, use the language's idiomatic name."""
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    target = resolve_target_test_file(source, adapter, TestConventions(), [], tests_dir, tmp_path)
    assert target.path == tests_dir / "test_calc.py"
    assert target.is_new_file is True
    assert target.ambiguous is False
    assert "no naming convention" in target.reason


def test_resolve_existing_file_on_disk(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ut = tests_dir / "test_calc.py"
    ut.write_text("def test_add(): assert add(1, 2) == 3\n", encoding="utf-8")

    target = resolve_target_test_file(
        source,
        adapter,
        TestConventions(),
        [],
        tests_dir,
        tmp_path,
    )
    assert target.path == ut
    assert target.is_new_file is False
    assert "test_add" in target.content


def test_resolve_targets_preferred_folder_when_no_test_dir_exists(
    tmp_path: Path,
) -> None:
    """Without a test folder, new tests belong in <root>/UnitTests/Code/tests."""
    adapter = PythonAdapter()
    root = tmp_path / "repo"
    src_dir = root / "src"
    src_dir.mkdir(parents=True)
    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    target = resolve_target_test_file(source, adapter, TestConventions(), [], None, root)

    assert target.path == root / "UnitTests" / "Code" / "tests" / "test_calc.py"
    assert target.is_new_file is True
    assert not target.path.parent.exists()  # created only when the test is written
