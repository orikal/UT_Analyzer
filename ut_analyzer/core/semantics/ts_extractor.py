"""Public entry point for semantic extraction.

Tree-sitter is the primary mechanism; :mod:`regex_fallback` only runs when no
grammar is available for the language, and the result says so.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import logging

from ...models.semantics import ExtractionSource, FileSemantics
from . import ts_support as ts
from .extractor_base import BaseExtractor
from .regex_fallback import extract_with_regex
from .rules_clike import CSharpExtractor, JavaExtractor
from .rules_cpp import CppExtractor
from .rules_go import GoExtractor
from .rules_js import JavaScriptExtractor, TypeScriptExtractor
from .rules_python import PythonExtractor

_EXTRACTORS: dict[str, type[BaseExtractor]] = {
    "c": CppExtractor,
    "cpp": CppExtractor,
    "csharp": CSharpExtractor,
    "go": GoExtractor,
    "java": JavaExtractor,
    "javascript": JavaScriptExtractor,
    "python": PythonExtractor,
    "typescript": TypeScriptExtractor,
}

_CACHE_LIMIT = 512
_CACHE: "OrderedDict[tuple, FileSemantics]" = OrderedDict()
_log = logging.getLogger(__name__)


def supported_languages() -> frozenset[str]:
    """Languages that have a real parser behind them."""
    if not ts.tree_sitter_available():
        return frozenset()
    return frozenset(_EXTRACTORS)


def extract(content: str, language: str, path: Path | None = None) -> FileSemantics:
    """Semantic facts for ``content``. Never raises; degrades to regex."""
    key = (language, str(path), len(content), hash(content))
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    result = _extract_uncached(content, language, path)
    _CACHE[key] = result
    if len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return result


def extract_file(path: Path, language: str, content: str | None = None) -> FileSemantics:
    """Semantic facts for a file on disk (reads it when ``content`` is None)."""
    if content is None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return FileSemantics(path=path, language=language, parse_ok=False)
    return extract(content, language, path)


def clear_cache() -> None:
    _CACHE.clear()


def _extract_uncached(content: str, language: str, path: Path | None) -> FileSemantics:
    extractor_type = _EXTRACTORS.get(language)
    if extractor_type is not None and ts.supported_language(language):
        parsed = ts.parse(content, language)
        if parsed is not None:
            root, data = _best_parse(content, language, extractor_type, parsed)
            try:
                return extractor_type(path, data).run(root)
            except Exception:  # pragma: no cover - a grammar quirk must not kill a scan
                _log.exception("semantic extraction failed for %s (%s)", path, language)
    fallback = extract_with_regex(content, language, path)
    if fallback.source is ExtractionSource.NONE:  # pragma: no cover - defensive
        fallback.source = ExtractionSource.REGEX_FALLBACK
    return fallback


def _best_parse(
    content: str,
    language: str,
    extractor_type: type[BaseExtractor],
    parsed: tuple,
) -> tuple:
    """Re-parse sanitized source when macros broke the first attempt.

    A macro such as ``Q_OBJECT`` makes the C++ grammar swallow the declaration
    below it, so real methods vanish. The sanitized source keeps every byte
    offset, and the retry is kept only when it really parses better.
    """
    root, _ = parsed
    if not ts.has_error(root):
        return parsed
    cleaned = extractor_type.sanitize(content)
    if cleaned == content:
        return parsed
    retry = ts.parse(cleaned, language)
    if retry is None:
        return parsed
    if ts.error_count(retry[0]) >= ts.error_count(root):
        return parsed
    return retry
