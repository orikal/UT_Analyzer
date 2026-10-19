"""Parsing helpers, including optional tree-sitter based syntax checking."""

from __future__ import annotations

import re

# tree-sitter is optional; everything degrades gracefully without it.
try:
    from tree_sitter_language_pack import get_parser  # type: ignore

    _TS_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    get_parser = None  # type: ignore
    _TS_AVAILABLE = False

# Map adapter language ids to tree-sitter language names.
_TS_LANGUAGE_NAMES = {
    "cpp": "cpp",
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "java": "java",
    "csharp": "csharp",
    "go": "go",
}


def tree_sitter_available() -> bool:
    return _TS_AVAILABLE


def check_syntax_tree_sitter(code: str, language: str) -> tuple[bool, str]:
    """Parse ``code`` with tree-sitter and report the first error location.

    Returns (ok, message). When tree-sitter is unavailable returns (True, note)
    so callers treat the check as inconclusive rather than failed.
    """
    ts_name = _TS_LANGUAGE_NAMES.get(language)
    if not _TS_AVAILABLE or ts_name is None:
        return True, "syntax check skipped (tree-sitter unavailable)"
    try:
        parser = get_parser(ts_name)  # type: ignore[misc]
        tree = parser.parse(code.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return True, f"syntax check skipped ({exc})"

    error_node = _find_error_node(tree.root_node)
    if error_node is None:
        return True, ""
    row, col = error_node.start_point
    snippet = code.encode("utf-8")[error_node.start_byte : error_node.end_byte]
    text = snippet.decode("utf-8", "replace")[:60]
    return False, f"syntax error near line {row + 1}, column {col + 1}: {text!r}"


def _find_error_node(node):  # type: ignore[no-untyped-def]
    if node.type == "ERROR" or getattr(node, "is_missing", False):
        return node
    if not node.has_error:
        return None
    for child in node.children:
        found = _find_error_node(child)
        if found is not None:
            return found
    return None


def count_matches(pattern: str, text: str, flags: int = 0) -> int:
    return len(re.findall(pattern, text, flags))


def normalize_code(code: str) -> str:
    """Normalize code for duplicate comparison: strip comments-ish and whitespace."""
    code = re.sub(r"//[^\n]*|#[^\n]*|/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"\s+", " ", code)
    return code.strip().lower()


def similarity(a: str, b: str) -> float:
    """Cheap similarity ratio between two normalized code strings."""
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio()
