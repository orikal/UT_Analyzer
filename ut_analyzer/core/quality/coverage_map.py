"""Evidence-based mapping from source symbols to the tests that exercise them.

This replaces the old ``function_name in test_source`` substring check, which
reported ``read``/``start``/``get`` as covered whenever those words appeared
anywhere in a test file - including in unrelated identifiers or comments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

from ...models.semantics import (
    CALLABLE_KINDS,
    TYPE_LIKE_KINDS,
    CallSite,
    Evidence,
    FileSemantics,
    SymbolDecl,
    SymbolKind,
)
from ...core.semantics.symbol_resolver import SymbolResolver

#: Names that carry no identifying information on their own. A call to ``get``
#: says nothing about *which* ``get`` was called, so a bare name match against
#: one of these can never be better than :attr:`CoverageConfidence.UNCERTAIN`.
AMBIGUOUS_NAMES = frozenset(
    {
        "get", "set", "read", "write", "start", "stop", "run", "init",
        "initialize", "update", "open", "close", "size", "count", "length",
        "clear", "reset", "add", "remove", "erase", "insert", "next", "prev",
        "value", "data", "name", "at", "begin", "end", "empty", "find",
        "contains", "push", "pop", "front", "back", "first", "last", "load",
        "save", "send", "receive", "process", "handle", "execute", "apply",
        "create", "destroy", "release", "acquire", "lock", "unlock", "check",
        "validate", "parse", "format", "print", "log", "tostring", "str",
        "repr", "equals", "hash", "compare", "clone", "copy", "swap", "main",
    }
)


class CoverageConfidence(str, Enum):
    """How strongly the evidence ties a source symbol to a test."""

    CONFIRMED = "confirmed"
    """A test calls the symbol on a receiver of the owning type, or calls a
    uniquely-named free function. There is no other symbol it could be."""

    LIKELY = "likely"
    """A test calls a symbol with this name and the owning type appears in the
    same test file, but the receiver could not be tied to the declaration."""

    UNCERTAIN = "uncertain"
    """Only a weak match exists: a common name, or a name mentioned in a test
    title. Never treat this as covered."""

    NOT_FOUND = "not_found"
    """No call to this symbol was parsed in any related test."""

    @property
    def is_covered(self) -> bool:
        """Only CONFIRMED and LIKELY count as covered."""
        return self in (CoverageConfidence.CONFIRMED, CoverageConfidence.LIKELY)


@dataclass(slots=True)
class SymbolCoverage:
    """Coverage verdict for a single source symbol."""

    symbol: SymbolDecl
    confidence: CoverageConfidence = CoverageConfidence.NOT_FOUND
    reason: str = ""
    tests: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.symbol.name

    @property
    def qualified_name(self) -> str:
        return self.symbol.qualified_name


@dataclass(slots=True)
class FileCoverage:
    """Coverage verdicts for every testable symbol of one source file."""

    source: Path | None = None
    entries: list[SymbolCoverage] = field(default_factory=list)

    def _of(self, *levels: CoverageConfidence) -> list[SymbolCoverage]:
        return [e for e in self.entries if e.confidence in levels]

    @property
    def covered(self) -> list[SymbolCoverage]:
        return self._of(CoverageConfidence.CONFIRMED, CoverageConfidence.LIKELY)

    @property
    def uncertain(self) -> list[SymbolCoverage]:
        return self._of(CoverageConfidence.UNCERTAIN)

    @property
    def untested(self) -> list[SymbolCoverage]:
        return self._of(CoverageConfidence.NOT_FOUND)

    def confidence_of(self, name: str) -> CoverageConfidence:
        for entry in self.entries:
            if entry.name == name or entry.qualified_name == name:
                return entry.confidence
        return CoverageConfidence.NOT_FOUND

    @staticmethod
    def _names(
        entries: list[SymbolCoverage], public_only: bool
    ) -> list[str]:
        return [
            e.qualified_name
            for e in entries
            if not public_only or e.symbol.is_public
        ]

    def untested_names(self, *, public_only: bool = False) -> list[str]:
        return self._names(self.untested, public_only)

    def uncertain_names(self, *, public_only: bool = False) -> list[str]:
        return self._names(self.uncertain, public_only)

    def covered_names(self, *, public_only: bool = False) -> list[str]:
        return self._names(self.covered, public_only)

    def all_names(self, *, public_only: bool = False) -> list[str]:
        return self._names(self.entries, public_only)

    def non_public_names(self) -> list[str]:
        return [e.qualified_name for e in self.entries if not e.symbol.is_public]


class TestCoverageMapper:
    """Decide, from parsed call sites, which source symbols tests exercise."""

    def __init__(self, resolver: SymbolResolver | None = None) -> None:
        self._resolver = resolver

    def map_file(
        self,
        source: FileSemantics,
        tests: Sequence[FileSemantics],
        *,
        include_non_public: bool = False,
    ) -> FileCoverage:
        """Verdicts for every callable declared in ``source``.

        ``include_non_public`` also reports private/protected members, which the
        UI needs so the user can ask for tests on them.
        """
        coverage = FileCoverage(source=source.path)
        targets = _testable(source, include_non_public=include_non_public)
        if not targets:
            return coverage
        types = self._known_types(source)
        index = _TestIndex(tests, types)
        for symbol in targets:
            coverage.entries.append(self._verdict(symbol, index, types))
        return coverage

    def _known_types(self, source: FileSemantics) -> set[str]:
        """Names that really are types, so namespaces are not mistaken for owners."""
        names = {t.name for t in source.types()}
        if self._resolver is not None:
            names |= {
                s.name
                for symbol in source.symbols
                if symbol.owner
                for s in self._resolver.all_named(symbol.owner)
                if s.kind in TYPE_LIKE_KINDS
            }
        return names

    # ------------------------------------------------------------ verdict

    def _verdict(
        self, symbol: SymbolDecl, index: "_TestIndex", types: set[str]
    ) -> SymbolCoverage:
        calls = index.calls_named(symbol.name)
        if not calls:
            return SymbolCoverage(
                symbol,
                CoverageConfidence.NOT_FOUND,
                reason="no test calls this symbol",
            )

        owner = symbol.owner if symbol.owner in types else ""
        confirmed = [c for c in calls if index.receiver_matches(c, owner)] if owner else []
        if confirmed:
            return self._result(
                symbol,
                CoverageConfidence.CONFIRMED,
                confirmed,
                index,
                f"called on a receiver of type {owner}",
            )

        if not owner and self._globally_unique(symbol):
            direct = [c for c in calls if not c.qualifier]
            if direct:
                return self._result(
                    symbol,
                    CoverageConfidence.CONFIRMED,
                    direct,
                    index,
                    "called directly and the name is unique in the repository",
                )

        if _is_ambiguous(symbol.name) or not self._globally_unique(symbol):
            return self._result(
                symbol,
                CoverageConfidence.UNCERTAIN,
                calls,
                index,
                f"'{symbol.name}' is used by more than one declaration; "
                "the call could belong to another type",
            )

        if owner and index.constructs(owner):
            return self._result(
                symbol,
                CoverageConfidence.LIKELY,
                calls,
                index,
                f"{owner} is constructed in the same test file",
            )

        return self._result(
            symbol,
            CoverageConfidence.LIKELY,
            calls,
            index,
            "a uniquely named symbol with this name is called by a test",
        )

    def _result(
        self,
        symbol: SymbolDecl,
        confidence: CoverageConfidence,
        calls: Sequence[CallSite],
        index: "_TestIndex",
        reason: str,
    ) -> SymbolCoverage:
        return SymbolCoverage(
            symbol=symbol,
            confidence=confidence,
            reason=reason,
            tests=sorted({c.enclosing for c in calls if c.enclosing}),
            evidence=[
                Evidence(file=c.file, line=c.line, snippet=c.text, kind="test call")
                for c in calls[:5]
            ],
        )

    def _globally_unique(self, symbol: SymbolDecl) -> bool:
        if self._resolver is None:
            return not _is_ambiguous(symbol.name)
        others = {
            s.qualified_name
            for s in self._resolver.all_named(symbol.name)
            if s.kind in CALLABLE_KINDS
        }
        return len(others) <= 1


class _TestIndex:
    """Call sites and constructions found across a set of parsed test files."""

    def __init__(self, tests: Sequence[FileSemantics], types: set[str] | None = None) -> None:
        self._by_name: dict[str, list[CallSite]] = {}
        self._constructed: set[str] = set()
        #: receiver variable -> type it was constructed from, per test file
        self._receiver_types: dict[str, set[str]] = {}
        known_types = types or set()
        for semantics in tests:
            for call in semantics.calls:
                # In dynamically typed languages a constructor call looks like
                # any other call, so a known type name is what identifies it.
                if call.is_construction or call.name in known_types:
                    self._constructed.add(call.name)
                    if call.target:
                        self._receiver_types.setdefault(call.target, set()).add(call.name)
                    if call.is_construction:
                        continue
                self._by_name.setdefault(call.name, []).append(call)
            for symbol in semantics.symbols:
                if symbol.kind in (SymbolKind.FIELD, SymbolKind.VARIABLE):
                    for type_name in _type_names(symbol.return_type or symbol.value):
                        self._receiver_types.setdefault(symbol.name, set()).add(type_name)
            for access in semantics.member_accesses:
                self._by_name.setdefault(access.name, []).append(
                    CallSite(
                        name=access.name,
                        qualifier=access.qualifier,
                        line=access.line,
                        enclosing=access.enclosing,
                        file=access.file,
                    )
                )

    def calls_named(self, name: str) -> list[CallSite]:
        return self._by_name.get(name, [])

    def constructs(self, type_name: str) -> bool:
        return type_name.rsplit("::", 1)[-1].rsplit(".", 1)[-1] in self._constructed

    def receiver_matches(self, call: CallSite, owner: str) -> bool:
        """True when the call's receiver is known to hold an ``owner`` value."""
        if not call.qualifier:
            return False
        short = owner.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        receiver = call.qualifier.strip().lstrip("*&")
        if receiver in (short, owner) or receiver.endswith(f"::{short}"):
            return True
        base = receiver.split("->")[-1].split(".")[-1].split("(")[0].strip()
        return short in self._receiver_types.get(base, set())


def _testable(
    source: FileSemantics, *, include_non_public: bool = False
) -> list[SymbolDecl]:
    """Callables worth reporting on, excluding destructors and the tests themselves."""
    symbols = [
        s
        for s in source.symbols
        if s.kind in CALLABLE_KINDS
        and s.kind is not SymbolKind.DESTRUCTOR
        and (include_non_public or s.is_public)
        and s.name
        and not s.name.startswith("~")
    ]
    return _dedupe(symbols)


def _dedupe(symbols: list[SymbolDecl]) -> list[SymbolDecl]:
    """One entry per qualified name.

    A C++ method is declared in the class and defined again out of line; the
    out-of-line definition carries no access specifier, so the in-class
    declaration (which does) wins.
    """
    order: list[str] = []
    best: dict[str, SymbolDecl] = {}
    for symbol in symbols:
        key = symbol.qualified_name
        current = best.get(key)
        if current is None:
            best[key] = symbol
            order.append(key)
        elif current.is_public and not symbol.is_public:
            best[key] = symbol
    return [best[key] for key in order]


def _is_ambiguous(name: str) -> bool:
    """True for names that identify nothing on their own."""
    return name.lower().lstrip("_") in AMBIGUOUS_NAMES


def _type_names(text: str) -> set[str]:
    """Type identifiers mentioned in a declaration such as ``unique_ptr<Foo>``."""
    token = ""
    names: set[str] = set()
    for char in text:
        if char.isalnum() or char == "_":
            token += char
        else:
            if token and token[0].isalpha():
                names.add(token)
            token = ""
    if token and token[0].isalpha():
        names.add(token)
    return names
