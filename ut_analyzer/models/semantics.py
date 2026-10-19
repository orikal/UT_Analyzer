"""Language-independent semantic facts extracted from real source files.

These models are the deterministic backbone of the analyzer: everything that
claims "this symbol exists / is called / is constructed here" must be able to
point at a :class:`SymbolDecl` or a :class:`CallSite` that a parser produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Scope separators per language id.
_SCOPE_SEPARATORS = {
    "cpp": "::",
    "csharp": ".",
    "java": ".",
    "python": ".",
    "go": ".",
    "javascript": ".",
    "typescript": ".",
}

DEFAULT_SCOPE_SEPARATOR = "."


def scope_separator(language: str) -> str:
    """The token this language uses between a scope and a member name."""
    return _SCOPE_SEPARATORS.get(language, DEFAULT_SCOPE_SEPARATOR)


class ExtractionSource(str, Enum):
    """Where a semantic fact came from. Parser data always wins over regex."""

    TREE_SITTER = "tree-sitter"
    NATIVE_AST = "native-ast"
    REGEX_FALLBACK = "regex"
    NONE = "none"

    @property
    def is_parser(self) -> bool:
        return self in (ExtractionSource.TREE_SITTER, ExtractionSource.NATIVE_AST)


class SymbolKind(str, Enum):
    """Kinds of declarations the extractors can recognize."""

    NAMESPACE = "namespace"
    MODULE = "module"
    CLASS = "class"
    STRUCT = "struct"
    INTERFACE = "interface"
    ENUM = "enum"
    ENUM_MEMBER = "enum_member"
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    DESTRUCTOR = "destructor"
    FIELD = "field"
    PROPERTY = "property"
    CONSTANT = "constant"
    VARIABLE = "variable"
    MACRO = "macro"
    ALIAS = "alias"
    TEST_CASE = "test_case"
    FIXTURE = "fixture"
    MOCK = "mock"
    UNKNOWN = "unknown"


#: Kinds that describe a type that can be instantiated or inherited from.
TYPE_LIKE_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.STRUCT,
        SymbolKind.INTERFACE,
        SymbolKind.ENUM,
        SymbolKind.ALIAS,
    }
)

#: Kinds that describe something callable.
CALLABLE_KINDS = frozenset(
    {
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.CONSTRUCTOR,
        SymbolKind.DESTRUCTOR,
        SymbolKind.MACRO,
    }
)


@dataclass(frozen=True)
class Evidence:
    """A concrete place in the repository that backs a claim."""

    file: Path | None = None
    line: int = 0
    snippet: str = ""
    kind: str = "declaration"

    def label(self) -> str:
        """Short human/LLM readable location, e.g. ``Foo.h:42``."""
        if self.file is None:
            return "unknown location"
        if self.line > 0:
            return f"{self.file.name}:{self.line}"
        return self.file.name

    def to_prompt_line(self) -> str:
        text = f"{self.kind} at {self.label()}"
        if self.snippet:
            return f"{text}: {self.snippet.strip()}"
        return text


@dataclass
class ParamDecl:
    """One declared parameter."""

    name: str = ""
    type_name: str = ""
    default: str = ""
    text: str = ""

    @property
    def is_optional(self) -> bool:
        return bool(self.default)

    def render(self) -> str:
        return self.text or " ".join(p for p in (self.type_name, self.name) if p)


@dataclass
class SymbolDecl:
    """A declaration found by a parser (or, as a last resort, by regex)."""

    kind: SymbolKind
    name: str
    file: Path | None = None
    line: int = 0
    end_line: int = 0
    scope: str = ""
    language: str = ""
    signature: str = ""
    params: list[ParamDecl] = field(default_factory=list)
    return_type: str = ""
    base_types: list[str] = field(default_factory=list)
    access: str = "public"
    is_static: bool = False
    is_abstract: bool = False
    decorators: list[str] = field(default_factory=list)
    template_params: list[str] = field(default_factory=list)
    value: str = ""
    members: list[str] = field(default_factory=list)
    body: str = ""
    source: ExtractionSource = ExtractionSource.NONE

    @property
    def qualified_name(self) -> str:
        if not self.scope:
            return self.name
        return f"{self.scope}{scope_separator(self.language)}{self.name}"

    @property
    def owner(self) -> str:
        """Innermost enclosing type/namespace name (``""`` at file scope)."""
        if not self.scope:
            return ""
        return self.scope.split(scope_separator(self.language))[-1]

    @property
    def is_public(self) -> bool:
        return self.access == "public"

    @property
    def required_arity(self) -> int:
        return sum(1 for p in self.params if not p.is_optional)

    def arity_range(self) -> tuple[int, int]:
        """Smallest and largest number of arguments this declaration accepts."""
        return self.required_arity, len(self.params)

    def accepts(self, arg_count: int) -> bool:
        low, high = self.arity_range()
        return low <= arg_count <= high

    def evidence(self, kind: str = "declaration") -> Evidence:
        return Evidence(
            file=self.file,
            line=self.line,
            snippet=self.signature or self.name,
            kind=kind,
        )


@dataclass
class CallSite:
    """A call (or construction) observed inside a file."""

    name: str
    qualifier: str = ""
    line: int = 0
    enclosing: str = ""
    arg_count: int = 0
    args: list[str] = field(default_factory=list)
    is_construction: bool = False
    #: Variable this call's result is bound to, when it is (``auto x = f()``).
    #: Lets callers tie a later ``x.method()`` back to a known type.
    target: str = ""
    text: str = ""
    file: Path | None = None

    def evidence(self) -> Evidence:
        kind = "construction" if self.is_construction else "call"
        return Evidence(file=self.file, line=self.line, snippet=self.text, kind=kind)


@dataclass
class MemberAccess:
    """A ``obj.member`` / ``obj->member`` / ``Type::member`` reference."""

    name: str
    qualifier: str = ""
    line: int = 0
    enclosing: str = ""
    file: Path | None = None


@dataclass
class ImportDecl:
    """An import / include / using directive."""

    raw: str
    module: str = ""
    symbols: list[str] = field(default_factory=list)
    line: int = 0
    is_relative: bool = False
    file: Path | None = None

    @property
    def target_name(self) -> str:
        """Last path component of the imported module (``Foo`` for ``a/Foo.h``)."""
        cleaned = self.module.replace("\\", "/").rstrip("/")
        tail = cleaned.rsplit("/", 1)[-1]
        tail = tail.rsplit(".", 1)[0] if "." in tail else tail
        return tail


@dataclass
class FileSemantics:
    """Everything a parser could tell us about one file."""

    path: Path | None = None
    language: str = ""
    source: ExtractionSource = ExtractionSource.NONE
    parse_ok: bool = True
    symbols: list[SymbolDecl] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    member_accesses: list[MemberAccess] = field(default_factory=list)
    imports: list[ImportDecl] = field(default_factory=list)

    def of_kind(self, *kinds: SymbolKind) -> list[SymbolDecl]:
        wanted = set(kinds)
        return [s for s in self.symbols if s.kind in wanted]

    def find(self, name: str) -> list[SymbolDecl]:
        """All declarations whose simple or qualified name matches ``name``."""
        return [s for s in self.symbols if s.name == name or s.qualified_name == name]

    def types(self) -> list[SymbolDecl]:
        return [s for s in self.symbols if s.kind in TYPE_LIKE_KINDS]

    def callables(self) -> list[SymbolDecl]:
        return [s for s in self.symbols if s.kind in CALLABLE_KINDS]

    def tests(self) -> list[SymbolDecl]:
        return self.of_kind(SymbolKind.TEST_CASE)

    def names(self) -> set[str]:
        return {s.name for s in self.symbols}

    def called_names(self) -> set[str]:
        return {c.name for c in self.calls}

    def imported_modules(self) -> set[str]:
        return {i.module for i in self.imports if i.module}

    def calls_within(self, symbol: SymbolDecl) -> list[CallSite]:
        """Calls that happen inside the body of ``symbol`` (by line range)."""
        if symbol.end_line <= 0:
            return [c for c in self.calls if c.enclosing == symbol.qualified_name]
        return [
            c
            for c in self.calls
            if c.enclosing == symbol.qualified_name
            or symbol.line <= c.line <= symbol.end_line
        ]

    def accesses_within(self, symbol: SymbolDecl) -> list[MemberAccess]:
        if symbol.end_line <= 0:
            return [a for a in self.member_accesses if a.enclosing == symbol.qualified_name]
        return [
            a
            for a in self.member_accesses
            if a.enclosing == symbol.qualified_name
            or symbol.line <= a.line <= symbol.end_line
        ]

    def is_empty(self) -> bool:
        return not (self.symbols or self.calls or self.imports)
