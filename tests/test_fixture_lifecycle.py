"""Tests for learning how the project's test fixtures initialize themselves."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.core.conventions.fixture_lifecycle import analyze_fixture_lifecycle
from ut_analyzer.core.conventions.test_template import derive_test_template
from ut_analyzer.models import EnvironmentInfo, TestFile

_SEEK_UT_H = """#ifndef SEEKUT_H_
#define SEEKUT_H_

#include "gtest/gtest.h"
#include "seek.h"

class SeekUT : public ::testing::Test
{
protected:
    SeekUT();
    ~SeekUT();
    void Init();

    Seek* m_seek;
    Reader m_reader;
};

#endif  // SEEKUT_H_
"""

_SEEK_UT_CPP = """#include "seekUT.h"

SeekUT::SeekUT()
    : m_seek(nullptr)
    , m_reader("seek")
{
    m_seek = new Seek(m_reader);
    Init();
}

SeekUT::~SeekUT()
{
    delete m_seek;
}

void SeekUT::Init()
{
    m_seek->Open();
}

TEST_F(SeekUT, ReadsFirstRecord)
{
    EXPECT_TRUE(m_seek->Read());
}
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_learns_out_of_line_constructor_destructor_and_init() -> None:
    lifecycle = analyze_fixture_lifecycle(
        [("seekUT.h", _SEEK_UT_H), ("seekUT.cpp", _SEEK_UT_CPP)], "cpp"
    )

    assert lifecycle.uses_constructor
    assert lifecycle.uses_destructor
    assert lifecycle.defines_out_of_line
    assert not lifecycle.uses_setup
    assert lifecycle.init_methods == ("Init",)
    bodies = "\n".join(sample.code for sample in lifecycle.samples)
    assert "m_seek = new Seek(m_reader);" in bodies
    assert "delete m_seek;" in bodies
    assert "a fixture constructor" in lifecycle.describe()


def test_setup_teardown_style_is_learned_without_constructors() -> None:
    content = """#include "calcUT.h"

void CalcUT::SetUp()
{
    m_calc = new Calc();
}

void CalcUT::TearDown()
{
    delete m_calc;
}
"""
    lifecycle = analyze_fixture_lifecycle([("calcUT.cpp", content)], "cpp")

    assert lifecycle.uses_setup
    assert lifecycle.uses_teardown
    assert not lifecycle.uses_constructor
    assert not lifecycle.defines_out_of_line


def test_inline_constructor_is_not_reported_as_out_of_line() -> None:
    content = """class CalcUT : public ::testing::Test
{
protected:
    CalcUT()
    {
        m_calc = new Calc();
    }

    Calc* m_calc;
};
"""
    lifecycle = analyze_fixture_lifecycle([("calcUT.h", content)], "cpp")

    assert lifecycle.uses_constructor
    assert not lifecycle.defines_out_of_line


def test_production_helpers_are_not_mistaken_for_fixture_hooks() -> None:
    content = """#include "helpers.h"

Reader::Reader(const std::string& name)
{
    m_name = name;
}

void Reader::Initialize()
{
    m_open = true;
}
"""
    lifecycle = analyze_fixture_lifecycle([("helpers.cpp", content)], "cpp")

    assert lifecycle.is_empty


def test_a_single_odd_file_does_not_beat_the_house_style() -> None:
    ctor = "class AUT : public ::testing::Test\n{\n    AUT()\n    {\n        x = 1;\n    }\n};\n"
    setup = "void BUT::SetUp()\n{\n    y = 2;\n}\n"
    lifecycle = analyze_fixture_lifecycle(
        [
            ("aUT.cpp", ctor),
            ("bUT.cpp", ctor.replace("AUT", "CUT")),
            ("dUT.cpp", setup),
        ],
        "cpp",
    )

    assert lifecycle.uses_constructor
    assert not lifecycle.uses_setup


def test_python_fixture_lifecycle() -> None:
    content = """class CalcTest(unittest.TestCase):
    def setUp(self):
        self.calc = Calc()

    def test_add(self):
        self.assertEqual(self.calc.add(1, 2), 3)
"""
    lifecycle = analyze_fixture_lifecycle([("test_calc.py", content)], "python")

    assert lifecycle.uses_setup
    assert "self.calc = Calc()" in lifecycle.samples[0].code


def test_template_learns_lifecycle_from_neighbouring_tests(tmp_path: Path) -> None:
    ut_dir = tmp_path / "UnitTest"
    _write(ut_dir / "seekUT.h", _SEEK_UT_H)
    cpp = _write(ut_dir / "seekUT.cpp", _SEEK_UT_CPP)
    neighbour = TestFile(
        path=cpp, language="cpp", framework="gtest", content=_SEEK_UT_CPP
    )

    template = derive_test_template(ut_dir / "readerUT.cpp", tmp_path / "reader.cpp", [neighbour], "cpp")

    assert template.lifecycle is not None
    assert template.lifecycle.uses_constructor
    assert template.lifecycle.defines_out_of_line
    assert "fixture constructor" in template.describe()


def test_generated_fixture_follows_the_constructor_style(tmp_path: Path) -> None:
    ut_dir = tmp_path / "UnitTest"
    _write(ut_dir / "seekUT.h", _SEEK_UT_H)
    cpp = _write(ut_dir / "seekUT.cpp", _SEEK_UT_CPP)
    source = _write(tmp_path / "reader.cpp", "class Reader\n{\npublic:\n    void Open();\n};\n")
    neighbour = TestFile(
        path=cpp, language="cpp", framework="gtest", content=_SEEK_UT_CPP
    )

    adapter = CppAdapter()
    env = EnvironmentInfo(language="cpp", language_name="C++", test_framework="gtest")
    target = ut_dir / "readerUT.cpp"
    template = derive_test_template(target, source, [neighbour], "cpp")

    _, header = adapter.new_test_header(target, source, env, template)
    preamble = adapter.new_file_preamble(source, env, target, template)

    assert "    ReaderUT();" in header
    assert "    ~ReaderUT();" in header
    assert "    void Init();" in header
    assert "void SetUp() override" not in header

    assert '#include "readerUT.h"' in preamble
    assert "ReaderUT::ReaderUT()\n{\n}" in preamble
    assert "ReaderUT::~ReaderUT()\n{\n}" in preamble
    assert "void ReaderUT::Init()\n{\n}" in preamble


def test_generated_fixture_keeps_setup_teardown_without_a_template(tmp_path: Path) -> None:
    source = _write(tmp_path / "reader.cpp", "class Reader\n{\npublic:\n    void Open();\n};\n")
    adapter = CppAdapter()
    env = EnvironmentInfo(language="cpp", language_name="C++", test_framework="gtest")
    target = tmp_path / "UnitTest" / "readerUT.cpp"

    _, header = adapter.new_test_header(target, source, env, None)
    preamble = adapter.new_file_preamble(source, env, target, None)

    assert "    void SetUp() override {}" in header
    assert "    void TearDown() override {}" in header
    assert preamble == '#include "readerUT.h"\n'
