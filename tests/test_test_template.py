"""New test files copy the structure of the project's own tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ut_analyzer.adapters.cpp.adapter import CppAdapter
from ut_analyzer.adapters.python.adapter import PythonAdapter
from ut_analyzer.core.conventions.test_template import derive_test_template
from ut_analyzer.core.semantics import tree_sitter_available
from ut_analyzer.models import EnvironmentInfo, TestFile

pytestmark = pytest.mark.skipif(
    not tree_sitter_available(), reason="tree-sitter grammars are not installed"
)


def _test_file(path: Path, content: str, language: str = "cpp") -> TestFile:
    return TestFile(
        path=path, language=language, framework="", cases=[], content=content
    )


def test_template_learns_shared_includes_from_neighbours(tmp_path: Path) -> None:
    tests_dir = tmp_path / "test"
    tests_dir.mkdir()
    neighbours = [
        _test_file(
            tests_dir / "alphaUT.cpp",
            '#include "gtest/gtest.h"\n#include "ProjectFixtures.h"\n#include "alpha.h"\n'
            "TEST_F(AlphaUT, Works) { EXPECT_EQ(1, 1); }\n",
        ),
        _test_file(
            tests_dir / "betaUT.cpp",
            '#include "gtest/gtest.h"\n#include "ProjectFixtures.h"\n#include "beta.h"\n'
            "TEST_F(BetaUT, Works) { EXPECT_EQ(2, 2); }\n",
        ),
    ]

    template = derive_test_template(
        tests_dir / "gammaUT.cpp", tmp_path / "gamma.cpp", neighbours, "cpp"
    )

    assert '#include "gtest/gtest.h"' in template.preamble
    assert '#include "ProjectFixtures.h"' in template.preamble
    # Each neighbour's own subject header is specific to that file, not shared.
    assert not any("alpha.h" in line for line in template.preamble)
    assert template.case_macro == "TEST_F"


def test_template_is_empty_without_neighbours(tmp_path: Path) -> None:
    template = derive_test_template(
        tmp_path / "fooUT.cpp", tmp_path / "foo.cpp", [], "cpp"
    )
    assert template.is_empty
    assert "no neighbouring test files" in template.describe()


def test_cpp_header_no_longer_invents_project_support_headers(tmp_path: Path) -> None:
    """The old header hard-coded TestEnvironment.h and ManagersForUT.h."""
    adapter = CppAdapter()
    source = tmp_path / "seek.cpp"
    source.write_text("class Seek { public: int read(); };\n", encoding="utf-8")

    result = adapter.new_test_header(
        tmp_path / "seekUT.cpp", source, EnvironmentInfo(language="cpp")
    )
    assert result is not None
    _path, content = result
    assert "TestEnvironment.h" not in content
    assert "ManagersForUT.h" not in content


def test_cpp_header_uses_the_includes_the_project_actually_uses(tmp_path: Path) -> None:
    adapter = CppAdapter()
    source = tmp_path / "seek.cpp"
    source.write_text("class Seek { public: int read(); };\n", encoding="utf-8")
    tests_dir = tmp_path / "test"
    tests_dir.mkdir()
    neighbours = [
        _test_file(
            tests_dir / f"{name}UT.cpp",
            '#include "gtest/gtest.h"\n#include "OurFixtures.h"\n'
            f"TEST_F({name}UT, Works) {{ EXPECT_EQ(1, 1); }}\n",
        )
        for name in ("alpha", "beta")
    ]
    template = derive_test_template(
        tests_dir / "seekUT.cpp", source, neighbours, "cpp"
    )

    result = adapter.new_test_header(
        tests_dir / "seekUT.cpp", source, EnvironmentInfo(language="cpp"), template
    )
    assert result is not None
    _path, content = result
    assert '#include "OurFixtures.h"' in content
    assert '#include "gtest/gtest.h"' in content


def test_python_preamble_prefers_learned_imports(tmp_path: Path) -> None:
    adapter = PythonAdapter()
    source = tmp_path / "calc.py"
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    neighbours = [
        _test_file(
            tests_dir / f"test_{name}.py",
            f"import pytest\nfrom myapp.testing import build_client\n\n"
            f"def test_{name}():\n    assert True\n",
        )
        for name in ("alpha", "beta")
    ]
    template = derive_test_template(
        tests_dir / "test_calc.py", source, neighbours, "python"
    )

    preamble = adapter.new_file_preamble(
        source, EnvironmentInfo(language="python", test_framework="pytest"),
        tests_dir / "test_calc.py", template,
    )
    assert "from myapp.testing import build_client" in preamble
