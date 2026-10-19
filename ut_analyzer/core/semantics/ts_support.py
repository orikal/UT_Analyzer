"""Thin, dependency-tolerant helpers around tree-sitter parse trees.

Nothing outside this module should need to know that tree-sitter exists.
"""

from __future__ import annotations

from typing import Any, Iterator

try:  # tree-sitter is optional; the analyzer degrades to regex without it.
    from tree_sitter_language_pack import get_parser as _get_parser  # type: ignore

    _TS_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    _get_parser = None  # type: ignore
    _TS_AVAILABLE = False


#: Analyzer language id -> tree-sitter grammar name.
TS_LANGUAGE_NAMES: dict[str, str] = {
    "cpp": "cpp",
    "c": "c",
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "java": "java",
    "csharp": "csharp",
    "go": "go",
}

_PARSERS: dict[str, Any] = {}


def tree_sitter_available() -> bool:
    return _TS_AVAILABLE


def supported_language(language: str) -> bool:
    """Whether a grammar exists for this analyzer language id."""
    return _TS_AVAILABLE and language in TS_LANGUAGE_NAMES


def parse(code: str, language: str) -> tuple[Any, bytes] | None:
    """Parse ``code`` and return ``(root_node, source_bytes)``.

    Returns ``None`` when the grammar is unavailable or the parser blew up, so
    callers can fall back to a weaker extractor.
    """
    grammar = TS_LANGUAGE_NAMES.get(language)
    if not _TS_AVAILABLE or grammar is None:
        return None
    parser = _PARSERS.get(grammar)
    if parser is None:
        try:
            parser = _get_parser(grammar)  # type: ignore[misc]
        except Exception:  # pragma: no cover - defensive
            return None
        _PARSERS[grammar] = parser
    data = code.encode("utf-8", "replace")
    try:
        tree = parser.parse(data)
    except Exception:  # pragma: no cover - defensive
        return None
    return tree.root_node, data


def text_of(node: Any, data: bytes) -> str:
    """Exact source text of a node."""
    if node is None:
        return ""
    return data[node.start_byte : node.end_byte].decode("utf-8", "replace")


def one_line(node: Any, data: bytes, limit: int = 200) -> str:
    """Node text collapsed to a single line, clipped to ``limit``."""
    collapsed = " ".join(text_of(node, data).split())
    return collapsed[:limit]


def line_of(node: Any) -> int:
    return int(node.start_point[0]) + 1


def end_line_of(node: Any) -> int:
    return int(node.end_point[0]) + 1


def field(node: Any, name: str) -> Any | None:
    if node is None:
        return None
    try:
        return node.child_by_field_name(name)
    except Exception:  # pragma: no cover - defensive
        return None


def fields(node: Any, name: str) -> list[Any]:
    """Every child stored under the same field name (grammars allow repeats)."""
    if node is None:
        return []
    found: list[Any] = []
    for index, child in enumerate(node.children):
        try:
            if node.field_name_for_child(index) == name:
                found.append(child)
        except Exception:  # pragma: no cover - defensive
            continue
    return found


def named_children(node: Any) -> list[Any]:
    if node is None:
        return []
    return [c for c in node.children if c.is_named]


def children_of_type(node: Any, *types: str) -> list[Any]:
    wanted = set(types)
    return [c for c in named_children(node) if c.type in wanted]


def first_of_type(node: Any, *types: str) -> Any | None:
    found = children_of_type(node, *types)
    return found[0] if found else None


def descendants(node: Any, *types: str) -> Iterator[Any]:
    """Depth-first iteration over descendants with one of ``types``."""
    wanted = set(types)
    stack = list(node.children)
    while stack:
        current = stack.pop()
        if current.type in wanted:
            yield current
        stack.extend(current.children)


def has_error(node: Any) -> bool:
    try:
        return bool(node.has_error)
    except Exception:  # pragma: no cover - defensive
        return False


def error_count(node: Any) -> int:
    """How many ERROR/MISSING nodes the tree holds (a parse-quality score)."""
    total = 0
    stack = [node]
    while stack:
        current = stack.pop()
        try:
            if current.type == "ERROR" or current.is_missing:
                total += 1
            if not current.has_error:
                continue
        except Exception:  # pragma: no cover - defensive
            continue
        stack.extend(current.children)
    return total
