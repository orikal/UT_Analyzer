"""Tree-sitter semantic extraction is the primary source of repository facts."""

from __future__ import annotations

from pathlib import Path

import pytest

from ut_analyzer.core.semantics import extract, tree_sitter_available
from ut_analyzer.core.semantics.regex_fallback import extract_with_regex
from ut_analyzer.models.semantics import ExtractionSource, SymbolKind

pytestmark = pytest.mark.skipif(
    not tree_sitter_available(), reason="tree-sitter grammars are not installed"
)

CPP_SOURCE = """
#include <memory>
#include "Seek.h"

#define MAX_LEN 10

namespace nsp {

enum class Mode : int { Fast = 1, Slow = 2 };

class Seek : public Base {
public:
    Seek(int a, const std::string& b);
    explicit Seek(int a);
    ~Seek();
    int read(int n) const;
    static void reset();
protected:
    int hidden_;
private:
    void secret();
};

int Seek::read(int n) const { return buffer_.at(n); }

}  // namespace nsp
"""

CPP_TEST = """
#include "SeekUT.h"

class SeekFixture : public ::testing::Test {
protected:
    void SetUp() override { seek_ = std::make_unique<nsp::Seek>(1, "x"); }
    std::unique_ptr<nsp::Seek> seek_;
};

class MockReader : public Reader {
public:
    MOCK_METHOD(int, read, (int), (override));
};

TEST_F(SeekFixture, ReadsRequestedBytes) {
    nsp::Seek seek(1, "payload");
    EXPECT_EQ(seek.read(2), 3);
}
"""

PYTHON_SOURCE = '''
from pkg.mod import Thing

MAX_LEN = 10


class Widget(Base):
    """A widget."""

    def __init__(self, a, b=2):
        self.a = a

    def read(self, n):
        return n

    def _secret(self):
        return 1


def helper(x) -> int:
    return x
'''


def _by_name(semantics, name):
    found = semantics.find(name)
    assert found, f"{name} was not extracted"
    return found[0]


def test_cpp_types_methods_and_constructors_are_parsed() -> None:
    semantics = extract(CPP_SOURCE, "cpp", Path("Seek.cpp"))

    assert semantics.source is ExtractionSource.TREE_SITTER
    seek = _by_name(semantics, "Seek")
    assert seek.kind is SymbolKind.CLASS
    assert seek.scope == "nsp"
    assert seek.qualified_name == "nsp::Seek"
    assert seek.base_types == ["Base"]

    constructors = [s for s in semantics.symbols if s.kind is SymbolKind.CONSTRUCTOR]
    assert {len(c.params) for c in constructors} == {1, 2}
    assert all(c.scope == "nsp::Seek" for c in constructors)

    read = [s for s in semantics.symbols if s.name == "read" and s.params]
    assert read and read[0].kind is SymbolKind.METHOD
    assert read[0].access == "public"

    hidden = _by_name(semantics, "hidden_")
    assert hidden.access == "protected"
    secret = _by_name(semantics, "secret")
    assert secret.access == "private"

    reset = _by_name(semantics, "reset")
    assert reset.is_static


def test_cpp_enum_members_and_macros_are_parsed() -> None:
    semantics = extract(CPP_SOURCE, "cpp", Path("Seek.cpp"))

    mode = _by_name(semantics, "Mode")
    assert mode.kind is SymbolKind.ENUM
    assert mode.members == ["Fast", "Slow"]

    fast = _by_name(semantics, "Fast")
    assert fast.kind is SymbolKind.ENUM_MEMBER
    assert fast.qualified_name == "nsp::Mode::Fast"

    macro = _by_name(semantics, "MAX_LEN")
    assert macro.kind is SymbolKind.MACRO

    assert {i.module for i in semantics.imports} == {"memory", "Seek.h"}


def test_cpp_test_macro_becomes_a_test_case_with_its_fixture() -> None:
    semantics = extract(CPP_TEST, "cpp", Path("SeekUT.cpp"))

    tests = semantics.tests()
    assert [t.name for t in tests] == ["ReadsRequestedBytes"]
    test = tests[0]
    assert test.scope == "SeekFixture"
    assert test.base_types == ["SeekFixture"]
    assert test.decorators == ["TEST_F"]

    fixture = _by_name(semantics, "SeekFixture")
    assert fixture.kind is SymbolKind.FIXTURE
    mock = _by_name(semantics, "MockReader")
    assert mock.kind is SymbolKind.MOCK


def test_cpp_constructions_inside_a_test_are_recorded() -> None:
    semantics = extract(CPP_TEST, "cpp", Path("SeekUT.cpp"))

    constructions = [c for c in semantics.calls if c.is_construction]
    by_name = {c.name: c for c in constructions}
    assert by_name["Seek"].arg_count == 2
    assert by_name["Seek"].qualifier == "nsp"
    assert by_name["Seek"].enclosing == "SeekFixture::ReadsRequestedBytes"

    read_calls = [c for c in semantics.calls if c.name == "read"]
    assert read_calls and read_calls[0].qualifier == "seek"


def test_cpp_deleted_constructor_is_not_reported() -> None:
    semantics = extract(
        "class Foo {\npublic:\n  Foo(const Foo&) = delete;\n  Foo(int a);\n};\n",
        "cpp",
        Path("Foo.h"),
    )
    constructors = [s for s in semantics.symbols if s.kind is SymbolKind.CONSTRUCTOR]
    assert len(constructors) == 1
    assert len(constructors[0].params) == 1


MACRO_CLASS = """
class MYLIB_API Worker : public QObject, public IWorker {
    Q_OBJECT
public:
    void start() override;
    virtual bool isBusy() const override;
    MYLIB_API int progress() const override;
signals:
    void finished();
public slots:
    void onTick() override;
};
"""


def test_cpp_macros_do_not_hide_override_methods() -> None:
    """``Q_OBJECT`` & co. used to make the grammar swallow the next declaration."""
    semantics = extract(MACRO_CLASS, "cpp", Path("worker.h"))
    methods = {
        s.qualified_name for s in semantics.symbols if s.kind is SymbolKind.METHOD
    }
    assert {
        "Worker::start",
        "Worker::isBusy",
        "Worker::progress",
        "Worker::onTick",
    } <= methods


def test_cpp_macro_sanitizer_keeps_source_offsets() -> None:
    from ut_analyzer.core.semantics.rules_cpp import sanitize_macros

    cleaned = sanitize_macros(MACRO_CLASS)
    assert len(cleaned) == len(MACRO_CLASS)
    assert cleaned.count("\n") == MACRO_CLASS.count("\n")
    assert "Q_OBJECT" not in cleaned
    assert "void start() override;" in cleaned


def test_cpp_macros_are_handled_in_crlf_files() -> None:
    """Windows sources keep their \\r, which the line-end patterns must allow."""
    semantics = extract(MACRO_CLASS.replace("\n", "\r\n"), "cpp", Path("worker.h"))
    methods = {
        s.qualified_name for s in semantics.symbols if s.kind is SymbolKind.METHOD
    }
    assert "Worker::start" in methods


def test_python_classes_methods_and_visibility() -> None:
    semantics = extract(PYTHON_SOURCE, "python", Path("widget.py"))

    widget = _by_name(semantics, "Widget")
    assert widget.kind is SymbolKind.CLASS
    assert widget.base_types == ["Base"]

    init = _by_name(semantics, "__init__")
    assert init.kind is SymbolKind.CONSTRUCTOR
    assert [p.name for p in init.params] == ["a", "b"]
    assert init.params[1].is_optional
    assert init.arity_range() == (1, 2)

    assert _by_name(semantics, "_secret").access == "private"
    assert _by_name(semantics, "helper").kind is SymbolKind.FUNCTION
    assert _by_name(semantics, "MAX_LEN").kind is SymbolKind.CONSTANT

    imported = semantics.imports[0]
    assert imported.module == "pkg.mod"
    assert imported.symbols == ["Thing"]


def test_python_fixtures_and_tests_are_classified() -> None:
    semantics = extract(
        "import pytest\n\n\n@pytest.fixture\ndef widget():\n    return Widget(1)\n\n\n"
        "def test_reads(widget):\n    assert widget.read(3) == 3\n",
        "python",
        Path("test_widget.py"),
    )

    assert [s.name for s in semantics.of_kind(SymbolKind.FIXTURE)] == ["widget"]
    assert [t.name for t in semantics.tests()] == ["test_reads"]
    read = [c for c in semantics.calls if c.name == "read"]
    assert read and read[0].qualifier == "widget"
    assert read[0].enclosing == "test_reads"


def test_java_annotations_drive_test_detection() -> None:
    semantics = extract(
        "package com.x;\nimport java.util.List;\n"
        "public class WidgetTest {\n"
        "    @Test\n    void readsBytes() { Widget w = new Widget(1); }\n"
        "}\n",
        "java",
        Path("WidgetTest.java"),
    )

    assert [t.name for t in semantics.tests()] == ["readsBytes"]
    assert semantics.tests()[0].scope == "com.x.WidgetTest"
    construction = [c for c in semantics.calls if c.is_construction]
    assert construction and construction[0].name == "Widget"
    assert construction[0].arg_count == 1


def test_csharp_attributes_and_enum_members() -> None:
    semantics = extract(
        "namespace Ns {\n"
        "  public enum Mode { Fast, Slow }\n"
        "  public class WidgetTests {\n"
        "    [Fact]\n    public void ReadsBytes() { var w = new Widget(1); }\n"
        "  }\n}\n",
        "csharp",
        Path("WidgetTests.cs"),
    )

    assert [t.name for t in semantics.tests()] == ["ReadsBytes"]
    mode = _by_name(semantics, "Mode")
    assert mode.members == ["Fast", "Slow"]
    assert _by_name(semantics, "Fast").qualified_name == "Ns.Mode.Fast"


def test_go_receiver_methods_and_tests() -> None:
    semantics = extract(
        'package widget\n\nconst Max = 3\n\ntype Widget struct { A int }\n\n'
        "func (w *Widget) Read(n int) int { return n }\n\n"
        'func TestRead(t *testing.T) { w := New(1); w.Read(1) }\n',
        "go",
        Path("widget.go"),
    )

    read = _by_name(semantics, "Read")
    assert read.kind is SymbolKind.METHOD
    assert read.scope == "widget.Widget"
    assert [t.name for t in semantics.tests()] == ["TestRead"]
    assert _by_name(semantics, "Max").kind is SymbolKind.CONSTANT


def test_javascript_describe_and_it_become_scoped_tests() -> None:
    semantics = extract(
        "import { Thing } from './thing';\n"
        "describe('Widget', () => {\n"
        "  it('reads', () => { const w = new Widget(1); expect(w.read(1)).toBe(1); });\n"
        "});\n",
        "javascript",
        Path("widget.test.js"),
    )

    tests = semantics.tests()
    assert [t.name for t in tests] == ["reads"]
    assert tests[0].scope == "Widget"
    assert semantics.imports[0].symbols == ["Thing"]


def test_parse_errors_are_reported_not_raised() -> None:
    semantics = extract("class Foo { int bar(", "cpp", Path("broken.cpp"))
    assert semantics.parse_ok is False


def test_regex_fallback_is_marked_as_untrusted() -> None:
    semantics = extract_with_regex(
        "class Widget:\n    def read(self, n):\n        return n\n",
        "python",
        Path("widget.py"),
    )
    assert semantics.source is ExtractionSource.REGEX_FALLBACK
    assert not semantics.source.is_parser
    assert {s.name for s in semantics.symbols} == {"Widget", "read"}
