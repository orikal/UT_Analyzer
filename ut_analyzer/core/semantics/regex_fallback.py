"""Best-effort regex extraction, used only when no grammar is available.

Parser output always wins: this module exists so an unsupported language (or a
machine without the tree-sitter wheels) still produces *something*, clearly
marked as :data:`ExtractionSource.REGEX_FALLBACK` so callers can distrust it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...models.semantics import (
    ExtractionSource,
    FileSemantics,
    ImportDecl,
    ParamDecl,
    SymbolDecl,
    SymbolKind,
)

_CPP_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import\s+([^\n]+)|import\s+([\w.,\s]+))", re.MULTILINE
)
_CLASS_RE = re.compile(
    r"^[ \t]*(?:public\s+|private\s+|internal\s+|abstract\s+|final\s+|sealed\s+)*"
    r"(class|struct|interface|enum)\s+(\w+)",
    re.MULTILINE,
)
_PY_CLASS_RE = re.compile(r"^[ \t]*class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", re.MULTILINE)
_PY_DEF_RE = re.compile(r"^([ \t]*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_FUNC_RE = re.compile(
    r"^[ \t]*(?:[\w:<>,\[\]\*&~\s]+?\s+)?(\w+)\s*\(([^;{)]*)\)\s*(?:const\s*)?[{;]",
    re.MULTILINE,
)
_CONST_RE = re.compile(
    r"^[ \t]*(?:static\s+|const\s+|constexpr\s+|final\s+|readonly\s+)+"
    r"[\w:<>\[\]\*&\s]*?\b([A-Z][A-Z0-9_]*)\s*=",
    re.MULTILINE,
)


def extract_with_regex(
    content: str, language: str, path: Path | None = None
) -> FileSemantics:
    """Produce a coarse :class:`FileSemantics` without a parser."""
    semantics = FileSemantics(
        path=path,
        language=language,
        source=ExtractionSource.REGEX_FALLBACK,
        parse_ok=True,
    )
    if language == "python":
        _python(content, semantics)
    else:
        _curly(content, semantics)
    return semantics


def _line_at(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _python(content: str, semantics: FileSemantics) -> None:
    for match in _PY_IMPORT_RE.finditer(content):
        module = match.group(1) or (match.group(3) or "").strip()
        symbols = [s.strip() for s in (match.group(2) or "").split(",") if s.strip()]
        semantics.imports.append(
            ImportDecl(
                raw=match.group(0).strip(),
                module=module.strip(),
                symbols=symbols,
                line=_line_at(content, match.start()),
                file=semantics.path,
            )
        )
    for match in _PY_CLASS_RE.finditer(content):
        semantics.symbols.append(
            _symbol(
                semantics,
                SymbolKind.CLASS,
                match.group(1),
                _line_at(content, match.start()),
                signature=match.group(0).strip(),
                base_types=[b.strip() for b in (match.group(2) or "").split(",") if b.strip()],
            )
        )
    for match in _PY_DEF_RE.finditer(content):
        name = match.group(2)
        indented = bool(match.group(1))
        params = _params(match.group(3))
        if indented and params and params[0].name in ("self", "cls"):
            params = params[1:]
        kind = SymbolKind.TEST_CASE if name.startswith("test_") else (
            SymbolKind.METHOD if indented else SymbolKind.FUNCTION
        )
        semantics.symbols.append(
            _symbol(
                semantics,
                kind,
                name,
                _line_at(content, match.start()),
                signature=match.group(0).strip(),
                params=params,
            )
        )


def _curly(content: str, semantics: FileSemantics) -> None:
    for match in _CPP_INCLUDE_RE.finditer(content):
        semantics.imports.append(
            ImportDecl(
                raw=match.group(0).strip(),
                module=match.group(1),
                line=_line_at(content, match.start()),
                file=semantics.path,
            )
        )
    kinds = {
        "class": SymbolKind.CLASS,
        "struct": SymbolKind.STRUCT,
        "interface": SymbolKind.INTERFACE,
        "enum": SymbolKind.ENUM,
    }
    for match in _CLASS_RE.finditer(content):
        semantics.symbols.append(
            _symbol(
                semantics,
                kinds[match.group(1)],
                match.group(2),
                _line_at(content, match.start()),
                signature=match.group(0).strip(),
            )
        )
    known = {s.name for s in semantics.symbols}
    for match in _FUNC_RE.finditer(content):
        name = match.group(1)
        if name in known or name in _KEYWORDS:
            continue
        semantics.symbols.append(
            _symbol(
                semantics,
                SymbolKind.FUNCTION,
                name,
                _line_at(content, match.start()),
                signature=match.group(0).strip().rstrip("{;"),
                params=_params(match.group(2)),
            )
        )
    for match in _CONST_RE.finditer(content):
        semantics.symbols.append(
            _symbol(
                semantics,
                SymbolKind.CONSTANT,
                match.group(1),
                _line_at(content, match.start()),
                signature=match.group(0).strip().rstrip("="),
            )
        )


_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "throw",
    "do", "else", "using", "namespace",
}


def _params(text: str) -> list[ParamDecl]:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    params: list[ParamDecl] = []
    for part in parts:
        name, _, default = part.partition("=")
        params.append(
            ParamDecl(
                name=name.split()[-1].lstrip("*&") if name.split() else "",
                text=part,
                default=default.strip(),
            )
        )
    return params


def _symbol(
    semantics: FileSemantics, kind: SymbolKind, name: str, line: int, **extra
) -> SymbolDecl:
    return SymbolDecl(
        kind=kind,
        name=name,
        file=semantics.path,
        line=line,
        language=semantics.language,
        source=ExtractionSource.REGEX_FALLBACK,
        **extra,
    )
