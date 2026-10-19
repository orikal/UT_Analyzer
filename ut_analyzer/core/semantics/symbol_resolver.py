"""Repository-wide symbol index built from parsed semantics.

Everything the generator is later allowed to claim about the repository has to
come through here, so "does this symbol exist?" is answered by parsed
declarations rather than by the model's memory.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

from ...models.semantics import (
    CALLABLE_KINDS,
    TYPE_LIKE_KINDS,
    Evidence,
    FileSemantics,
    SymbolDecl,
    SymbolKind,
    scope_separator,
)

_MOCK_PREFIXES = ("mock", "fake", "stub", "dummy", "spy")


class ResolutionStatus(str, Enum):
    """Outcome of looking a name up in the repository index."""

    RESOLVED = "resolved"
    """Exactly one declaration matches."""

    AMBIGUOUS = "ambiguous"
    """Several declarations match and the caller must disambiguate."""

    UNKNOWN = "unknown"
    """No declaration was parsed anywhere in the indexed sources."""


@dataclass(slots=True)
class Resolution:
    """The answer to "what is ``name``?" together with its evidence."""

    name: str
    status: ResolutionStatus
    candidates: list[SymbolDecl] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    @property
    def declaration(self) -> SymbolDecl | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    def evidence(self) -> list[Evidence]:
        return [c.evidence() for c in self.candidates]


class SymbolResolver:
    """Name -> declaration index over a set of parsed files."""

    def __init__(self, language: str = "") -> None:
        self.language = language
        self._files: dict[Path, FileSemantics] = {}
        self._by_name: dict[str, list[SymbolDecl]] = defaultdict(list)
        self._by_qualified: dict[str, list[SymbolDecl]] = defaultdict(list)
        self._members: dict[str, list[SymbolDecl]] = defaultdict(list)

    # ------------------------------------------------------------- build

    def add(self, semantics: FileSemantics) -> None:
        """Index one parsed file. Re-adding the same path replaces it."""
        if semantics.path is not None and semantics.path in self._files:
            return
        if semantics.path is not None:
            self._files[semantics.path] = semantics
        if not self.language and semantics.language:
            self.language = semantics.language
        for symbol in semantics.symbols:
            self._by_name[symbol.name].append(symbol)
            self._by_qualified[symbol.qualified_name].append(symbol)
            if symbol.scope:
                self._members[symbol.scope].append(symbol)
                self._members[symbol.scope.rsplit(self._sep(), 1)[-1]].append(symbol)

    def add_all(self, files: Iterable[FileSemantics]) -> None:
        for semantics in files:
            self.add(semantics)

    def _sep(self) -> str:
        return scope_separator(self.language)

    # ------------------------------------------------------------ lookup

    def resolve(self, name: str, *, kinds: frozenset[SymbolKind] | None = None) -> Resolution:
        """Look ``name`` up by qualified name first, then by bare name."""
        clean = name.strip().lstrip("*&")
        if not clean:
            return Resolution(name, ResolutionStatus.UNKNOWN)
        candidates = list(self._by_qualified.get(clean, ()))
        if not candidates:
            candidates = list(self._by_name.get(clean.rsplit(self._sep(), 1)[-1], ()))
        if kinds is not None:
            candidates = [c for c in candidates if c.kind in kinds]
        if not candidates:
            return Resolution(clean, ResolutionStatus.UNKNOWN)
        if len({c.qualified_name for c in candidates}) == 1:
            return Resolution(clean, ResolutionStatus.RESOLVED, candidates[:1])
        return Resolution(clean, ResolutionStatus.AMBIGUOUS, candidates)

    def is_known(self, name: str) -> bool:
        """True when *any* declaration with this name was parsed."""
        return self.resolve(name).status is not ResolutionStatus.UNKNOWN

    def is_unique(self, name: str) -> bool:
        """True when exactly one declaration in the repository owns this name."""
        return self.resolve(name).ok

    def declaration_of(
        self, name: str, *, kinds: frozenset[SymbolKind] | None = None
    ) -> SymbolDecl | None:
        resolution = self.resolve(name, kinds=kinds)
        return resolution.candidates[0] if resolution.candidates else None

    def all_named(self, name: str) -> list[SymbolDecl]:
        return list(self._by_name.get(name, ()))

    # --------------------------------------------------------- structure

    def type_of(self, name: str) -> SymbolDecl | None:
        return self.declaration_of(name, kinds=TYPE_LIKE_KINDS)

    def members_of(self, type_name: str) -> list[SymbolDecl]:
        """Fields and methods declared inside ``type_name``."""
        declaration = self.type_of(type_name)
        scope = declaration.qualified_name if declaration else type_name
        seen: set[tuple[str, int]] = set()
        members: list[SymbolDecl] = []
        for symbol in self._members.get(scope, ()) or self._members.get(type_name, ()):
            key = (symbol.qualified_name, symbol.line)
            if key not in seen:
                seen.add(key)
                members.append(symbol)
        return members

    def constructors_of(self, type_name: str) -> list[SymbolDecl]:
        """Declared constructors, ordered by arity (simplest first)."""
        declaration = self.type_of(type_name)
        short = (declaration.name if declaration else type_name).rsplit(self._sep(), 1)[-1]
        found = [
            m
            for m in self.members_of(type_name)
            if m.kind is SymbolKind.CONSTRUCTOR
            or (m.kind in CALLABLE_KINDS and m.name == short and not m.return_type)
        ]
        if not found and declaration is not None and self.language == "python":
            return []
        return sorted(found, key=lambda s: len(s.params))

    def overloads_of(self, name: str) -> list[SymbolDecl]:
        return [s for s in self.all_named(name) if s.kind in CALLABLE_KINDS]

    def enum_values_of(self, enum_name: str) -> list[SymbolDecl]:
        declaration = self.type_of(enum_name)
        if declaration is None or declaration.kind is not SymbolKind.ENUM:
            return []
        return [m for m in self.members_of(enum_name) if m.kind is SymbolKind.ENUM_MEMBER]

    def constants(self) -> list[SymbolDecl]:
        return [
            s
            for symbols in self._by_name.values()
            for s in symbols
            if s.kind in (SymbolKind.CONSTANT, SymbolKind.MACRO)
        ]

    def mocks_for(self, type_name: str) -> list[SymbolDecl]:
        """Existing test doubles that stand in for ``type_name``."""
        short = type_name.rsplit(self._sep(), 1)[-1]
        lowered = short.lower()
        found: list[SymbolDecl] = []
        for symbols in self._by_name.values():
            for symbol in symbols:
                if symbol.kind is not SymbolKind.MOCK and not _looks_like_mock(symbol.name):
                    continue
                if lowered in symbol.name.lower() or short in symbol.base_types:
                    found.append(symbol)
        return found

    def helpers_named(self, fragment: str) -> list[SymbolDecl]:
        lowered = fragment.lower()
        return [
            s
            for symbols in self._by_name.values()
            for s in symbols
            if s.kind in CALLABLE_KINDS and lowered in s.name.lower()
        ]

    # ------------------------------------------------------------- files

    def files(self) -> list[FileSemantics]:
        return list(self._files.values())

    def semantics_for(self, path: Path) -> FileSemantics | None:
        return self._files.get(path)

    def instantiations_in_tests(self, type_name: str) -> list[Evidence]:
        """How existing tests actually build ``type_name``."""
        short = type_name.rsplit(self._sep(), 1)[-1]
        evidence: list[Evidence] = []
        for semantics in self._files.values():
            if not semantics.tests():
                continue
            for call in semantics.calls:
                if call.is_construction and call.name == short:
                    evidence.append(
                        Evidence(
                            file=semantics.path,
                            line=call.line,
                            snippet=call.text,
                            kind="instantiation",
                        )
                    )
        return evidence

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_name.values())


def build_resolver(files: Sequence[FileSemantics], language: str = "") -> SymbolResolver:
    resolver = SymbolResolver(language)
    resolver.add_all(files)
    return resolver


def _looks_like_mock(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(_MOCK_PREFIXES) or lowered.endswith(_MOCK_PREFIXES)
