"""Generated UT file names must follow the naming style of the UT folder."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.adapters.python.adapter import PythonAdapter
from ut_analyzer.core.conventions.convention_analyzer import analyze_conventions
from ut_analyzer.core.discovery.test_file_resolver import (
    resolve_target_test_file,
    source_stem_from_test_stem,
    split_test_stem,
)
from ut_analyzer.models import EnvironmentInfo, TestFile


def _test_file(path: Path, language: str = "cpp") -> TestFile:
    return TestFile(path=path, language=language, content="")


def test_split_test_stem_keeps_affix_spelling() -> None:
    assert split_test_stem("SeekUT") == ("", "Seek", "UT")
    assert split_test_stem("seek_UT") == ("", "seek", "_UT")
    assert split_test_stem("UT_seek") == ("UT_", "seek", "")
    assert split_test_stem("test_calc") == ("test_", "calc", "")
    assert split_test_stem("calc_test") == ("", "calc", "_test")
    assert split_test_stem("SeekUnitTest") == ("", "Seek", "UnitTest")
    assert split_test_stem("calc.spec") == ("", "calc", ".spec")
    assert split_test_stem("calc") is None


def test_source_stem_from_test_stem_handles_any_marker() -> None:
    assert source_stem_from_test_stem("UT_Seek") == "seek"
    assert source_stem_from_test_stem("SeekUnitTest") == "seek"


def test_file_naming_pattern_follows_ut_folder(tmp_path: Path) -> None:
    ut_dir = tmp_path / "UnitTest"
    test_files = [
        _test_file(tmp_path / "misc" / "test_legacy.cpp"),
        _test_file(ut_dir / "seekUT.cpp"),
        _test_file(ut_dir / "readerUT.cpp"),
    ]
    conventions = analyze_conventions(test_files, "cpp", test_dir=ut_dir)
    assert conventions.file_naming == "<name>UT.cpp"


def test_file_naming_pattern_supports_prefix_convention(tmp_path: Path) -> None:
    ut_dir = tmp_path / "UT"
    test_files = [
        _test_file(ut_dir / "UT_seek.cpp"),
        _test_file(ut_dir / "UT_reader.cpp"),
    ]
    conventions = analyze_conventions(test_files, "cpp", test_dir=ut_dir)
    assert conventions.file_naming == "UT_<name>.cpp"


def test_generated_name_matches_existing_ut_files(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    ut_dir = tmp_path / "tests"
    ut_dir.mkdir()
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    source = src_dir / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    conventions = analyze_conventions(
        [_test_file(ut_dir / "reader_tests.py", "python")], "python", test_dir=ut_dir
    )
    target = resolve_target_test_file(source, adapter, conventions, [], ut_dir, tmp_path)
    assert target.path == ut_dir / "calc_tests.py"


def test_cpp_header_and_include_follow_the_ut_file_name(tmp_path: Path) -> None:
    adapter = CppAdapter()
    source = tmp_path / "seek.cpp"
    source.write_text("int seek() { return 0; }\n", encoding="utf-8")
    test_path = tmp_path / "UT" / "UT_seek.cpp"

    header = adapter.new_test_header(test_path, source, EnvironmentInfo())
    assert header is not None
    assert header[0] == test_path.parent / "UT_seek.h"
    assert adapter.new_file_preamble(source, EnvironmentInfo(), test_path) == (
        '#include "UT_seek.h"\n'
    )
