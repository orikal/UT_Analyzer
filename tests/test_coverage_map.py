"""Coverage is decided from parsed call sites, never from substring matches."""

from __future__ import annotations

from pathlib import Path

import pytest

from ut_analyzer.core.quality.coverage_map import (
    AMBIGUOUS_NAMES,
    CoverageConfidence,
    TestCoverageMapper,
)
from ut_analyzer.core.semantics import extract, tree_sitter_available
from ut_analyzer.core.semantics.symbol_resolver import (
    ResolutionStatus,
    SymbolResolver,
)

pytestmark = pytest.mark.skipif(
    not tree_sitter_available(), reason="tree-sitter grammars are not installed"
)

SOURCE = """
class Reader:
    def read(self, n):
        return n

    def rewind(self):
        return 0

    def decode_frame(self, payload):
        return payload
"""


def _map(source_code, test_code, source_name="reader.py", test_name="test_reader.py"):
    source = extract(source_code, "python", Path(source_name))
    tests = extract(test_code, "python", Path(test_name))
    resolver = SymbolResolver("python")
    resolver.add(source)
    resolver.add(tests)
    return TestCoverageMapper(resolver).map_file(source, [tests])


def test_a_common_name_appearing_in_a_test_is_not_treated_as_covered() -> None:
    """`read` is called, but on an unrelated object - that is not evidence."""
    coverage = _map(
        SOURCE,
        "def test_something():\n"
        "    handle = open('f')\n"
        "    handle.read(1)\n",
    )
    assert coverage.confidence_of("read") is CoverageConfidence.UNCERTAIN
    assert "read" not in [e.symbol.name for e in coverage.covered]


def test_mentioning_a_name_only_in_text_never_counts() -> None:
    """The old substring check reported this file as fully covered."""
    coverage = _map(
        SOURCE,
        "def test_documented():\n"
        "    # this exercises read, rewind and decode_frame\n"
        "    assert True\n",
    )
    assert coverage.covered == []
    assert {e.symbol.name for e in coverage.untested} == {
        "read",
        "rewind",
        "decode_frame",
    }


def test_call_on_a_receiver_of_the_owning_type_is_confirmed() -> None:
    coverage = _map(
        SOURCE,
        "def test_decodes():\n"
        "    reader = Reader()\n"
        "    assert reader.decode_frame(b'x') == b'x'\n",
    )
    entry = next(e for e in coverage.entries if e.name == "decode_frame")
    assert entry.confidence is CoverageConfidence.CONFIRMED
    assert entry.tests == ["test_decodes"]
    assert entry.evidence and entry.evidence[0].line > 0


def test_a_bare_call_to_a_method_name_is_only_likely() -> None:
    """Without a receiver there is no proof it is `Reader.decode_frame`."""
    coverage = _map(
        SOURCE,
        "def test_decodes():\n    assert decode_frame(b'x') == b'x'\n",
    )
    assert coverage.confidence_of("decode_frame") is CoverageConfidence.LIKELY


def test_a_name_declared_twice_can_only_be_uncertain() -> None:
    coverage = _map(
        SOURCE + "\n\nclass Writer:\n    def decode_frame(self, p):\n        return p\n",
        "def test_decodes():\n    assert decode_frame(b'x') == b'x'\n",
    )
    assert coverage.confidence_of("decode_frame") is CoverageConfidence.UNCERTAIN


def test_uncertain_symbols_are_reported_separately_from_untested() -> None:
    coverage = _map(
        SOURCE,
        "def test_reads():\n"
        "    handle = open('f')\n"
        "    handle.read(1)\n",
    )
    assert coverage.uncertain_names() == ["Reader.read"]
    assert "Reader.rewind" in coverage.untested_names()


def test_confidence_levels_declare_what_counts_as_covered() -> None:
    assert CoverageConfidence.CONFIRMED.is_covered
    assert CoverageConfidence.LIKELY.is_covered
    assert not CoverageConfidence.UNCERTAIN.is_covered
    assert not CoverageConfidence.NOT_FOUND.is_covered


def test_the_ambiguous_name_list_covers_the_reported_accessors() -> None:
    for name in ("get", "set", "read", "write", "start", "stop"):
        assert name in AMBIGUOUS_NAMES


def test_cpp_method_is_confirmed_through_a_constructed_receiver() -> None:
    source = extract(
        "class Seek {\npublic:\n  Seek(int a);\n  int read(int n);\n  int rewind();\n};\n",
        "cpp",
        Path("Seek.h"),
    )
    tests = extract(
        "TEST_F(SeekFixture, Reads) {\n  Seek seek(1);\n  EXPECT_EQ(seek.read(2), 2);\n}\n",
        "cpp",
        Path("SeekUT.cpp"),
    )
    resolver = SymbolResolver("cpp")
    resolver.add(source)
    resolver.add(tests)
    coverage = TestCoverageMapper(resolver).map_file(source, [tests])

    assert coverage.confidence_of("read") is CoverageConfidence.CONFIRMED
    assert coverage.confidence_of("rewind") is CoverageConfidence.NOT_FOUND


def test_resolver_reports_unknown_ambiguous_and_resolved_names() -> None:
    resolver = SymbolResolver("python")
    resolver.add(extract(SOURCE, "python", Path("reader.py")))

    assert resolver.resolve("Reader").status is ResolutionStatus.RESOLVED
    assert resolver.resolve("FooFactory").status is ResolutionStatus.UNKNOWN
    assert not resolver.is_known("FooFactory")

    resolver.add(
        extract("class Reader:\n    pass\n", "python", Path("other.py"))
    )
    assert resolver.resolve("Reader").status is ResolutionStatus.RESOLVED


def test_resolver_lists_constructors_with_their_arity() -> None:
    resolver = SymbolResolver("cpp")
    resolver.add(
        extract(
            "class Seek {\npublic:\n  Seek(int a);\n  Seek(int a, const char* b);\n};\n",
            "cpp",
            Path("Seek.h"),
        )
    )
    constructors = resolver.constructors_of("Seek")
    assert [len(c.params) for c in constructors] == [1, 2]
    assert constructors[0].arity_range() == (1, 1)


def test_resolver_finds_enum_values_and_existing_mocks() -> None:
    resolver = SymbolResolver("cpp")
    resolver.add(
        extract(
            "enum class Mode { Fast, Slow };\n"
            "class MockReader : public Reader { };\n",
            "cpp",
            Path("Defs.h"),
        )
    )
    assert [v.name for v in resolver.enum_values_of("Mode")] == ["Fast", "Slow"]
    assert [m.name for m in resolver.mocks_for("Reader")] == ["MockReader"]
