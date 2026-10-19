"""Tests for the post-generation guards: real constructors and required includes."""

from __future__ import annotations

from ut_analyzer.core.generation.symbol_context import (
    ContextFile,
    SymbolContext,
    condense_declarations,
    ensure_symbol_includes,
    extract_constructors,
    flag_unknown_constructions,
)

CPP_CLASS = """
class Seek
{
public:
    Seek(int id);
    void start();
};
"""


def _seek_constructors():
    return extract_constructors(
        condense_declarations(CPP_CLASS, "cpp"),
        "cpp",
        file_name="seek.h",
        include='#include "seek.h"',
    )


def test_invented_cpp_constructor_is_flagged() -> None:
    code = "TEST_F(SeekUT, Works)\n{\n    Seek seek(1, 2, 3);\n    EXPECT_TRUE(true);\n}\n"

    result = flag_unknown_constructions(code, _seek_constructors(), "cpp")

    assert "// TODO: Seek has no constructor taking 3 argument(s)" in result
    assert "use one of: Seek(int id)" in result
    assert "Seek seek(1, 2, 3);" in result  # the code itself is kept for review


def test_valid_cpp_constructor_is_untouched() -> None:
    code = "TEST_F(SeekUT, Works)\n{\n    Seek seek(1);\n}\n"

    assert flag_unknown_constructions(code, _seek_constructors(), "cpp") == code


def test_make_unique_with_wrong_arity_is_flagged() -> None:
    code = "    auto seek = std::make_unique<Seek>(1, 2);\n"

    result = flag_unknown_constructions(code, _seek_constructors(), "cpp")

    assert "TODO: Seek has no constructor taking 2 argument(s)" in result


def test_python_constructor_arity_is_flagged() -> None:
    module = "class Reader:\n    def __init__(self, path):\n        self.path = path\n"
    constructors = extract_constructors(
        condense_declarations(module, "python"), "python", file_name="reader.py"
    )
    code = "def test_reader():\n    reader = Reader('a', 'b')\n    assert reader\n"

    result = flag_unknown_constructions(code, constructors, "python")

    assert "# TODO: Reader has no constructor taking 2 argument(s)" in result


def _cpp_context() -> SymbolContext:
    return SymbolContext(
        files=[
            ContextFile(
                name="Types.h",
                declarations="enum class DdsState { Init };",
                include='#include "Types.h"',
            )
        ],
        symbols=[("enum", "DdsState", "Types.h")],
    )


def test_missing_include_is_added_to_ut_header() -> None:
    header = (
        "#ifndef SEEKUT_H\n"
        "#define SEEKUT_H\n\n"
        '#include "gtest/gtest.h"\n'
        '#include "seek.h"\n\n'
        "class SeekUT : public ::testing::Test\n"
        "{\n"
        "};\n\n"
        "#endif  // SEEKUT_H\n"
    )
    used = "EXPECT_EQ(DdsState::Init, seek.state());"

    result = ensure_symbol_includes(header, used, _cpp_context(), "cpp")

    assert '#include "Types.h"' in result
    assert result.index('#include "Types.h"') < result.index("class SeekUT")


def test_include_is_not_duplicated() -> None:
    header = '#include "Types.h"\n\nclass SeekUT {};\n'

    result = ensure_symbol_includes(header, "DdsState::Init", _cpp_context(), "cpp")

    assert result.count('#include "Types.h"') == 1


def test_unused_symbol_does_not_add_include() -> None:
    header = '#include "seek.h"\n\nclass SeekUT {};\n'

    result = ensure_symbol_includes(header, "EXPECT_TRUE(true);", _cpp_context(), "cpp")

    assert result == header


def test_missing_python_import_is_added_to_preamble() -> None:
    context = SymbolContext(
        files=[
            ContextFile(
                name="models.py",
                declarations="MAX_ITEMS = 32",
                include="from models import <symbol>",
            )
        ],
        symbols=[("constant", "MAX_ITEMS", "models.py")],
    )
    preamble = "import pytest\n\nfrom engine import run\n"

    result = ensure_symbol_includes(preamble, "assert run() == MAX_ITEMS", context, "python")

    assert "from models import MAX_ITEMS" in result
