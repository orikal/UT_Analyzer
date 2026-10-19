"""Deterministic semantic analysis of repository source code."""

from .ts_extractor import clear_cache, extract, extract_file, supported_languages
from .ts_support import tree_sitter_available

__all__ = [
    "clear_cache",
    "extract",
    "extract_file",
    "supported_languages",
    "tree_sitter_available",
]
