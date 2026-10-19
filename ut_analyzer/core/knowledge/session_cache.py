"""Persist the result of one scan so it can be reused without scanning again.

Every scan already writes a Markdown knowledge pack into ``<root>/.ut_analyzer``.
This module stores the *structured* result of the same scan next to it
(``analysis.json.gz``) so the UI can offer an "Analyze" action that reuses the
existing folder instead of walking, indexing and analyzing the repository again.

The payload is plain JSON (never pickle): decoding is driven by the dataclass
type hints of the models, so an unexpected or hand-edited file can only produce
wrong values, never executable code.
"""

from __future__ import annotations

import gzip
import json
import types
import typing
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from pathlib import Path

from ...models import (
    EnvironmentInfo,
    QualityReport,
    RepoKnowledge,
    ScanResult,
    TestConventions,
    TestFile,
    UTKnowledge,
)
from .knowledge_docs import KnowledgeDocs
from .repo_indexer import KNOWLEDGE_DIR_NAME, ensure_knowledge_dir, knowledge_dir_candidates

CACHE_FILE_NAME = "analysis.json.gz"
CACHE_VERSION = 1

#: Session attribute -> model class, used for both encoding and decoding.
_SECTIONS: dict[str, type] = {
    "scan": ScanResult,
    "env": EnvironmentInfo,
    "test_files": TestFile,          # list
    "conventions": TestConventions,
    "repo_knowledge": RepoKnowledge,
    "ut_knowledge": UTKnowledge,
    "knowledge_docs": KnowledgeDocs,
    "quality": QualityReport,
}
_LIST_SECTIONS = frozenset({"test_files"})


class CacheError(RuntimeError):
    """The saved analysis is missing, unreadable or written by another version."""


# ---- locations -------------------------------------------------------------


def cache_file_candidates(root: Path) -> list[Path]:
    """Every place the saved analysis of ``root`` may live, best first."""
    return [folder / CACHE_FILE_NAME for folder in knowledge_dir_candidates(root)]


def find_cached_analysis(root: Path) -> Path | None:
    """The saved analysis file for ``root``, or ``None`` when there is none."""
    for candidate in cache_file_candidates(root):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def knowledge_folder_exists(root: Path) -> bool:
    """True when the repository already has a ``.ut_analyzer`` folder."""
    try:
        return (root / KNOWLEDGE_DIR_NAME).is_dir()
    except OSError:
        return False


# ---- saving / loading ------------------------------------------------------


def save_analysis(session) -> Path | None:
    """Write the analysis of ``session`` next to its knowledge pack."""
    folder = ensure_knowledge_dir(session.config.root)
    if folder is None:
        return None
    payload: dict[str, object] = {
        "version": CACHE_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(session.config.root),
        "test_dir": str(session.config.test_dir) if session.config.test_dir else None,
    }
    for name in _SECTIONS:
        payload[name] = _encode(getattr(session, name, None))
    target = folder / CACHE_FILE_NAME
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        with gzip.open(target, "wb") as handle:
            handle.write(data)
    except OSError:
        return None
    return target


def load_analysis(root: Path) -> dict[str, object]:
    """Read the saved analysis of ``root`` back into model objects.

    Returns a mapping with the ``_SECTIONS`` keys plus ``saved_at`` and ``path``.
    Raises :class:`CacheError` when nothing usable is stored.
    """
    path = find_cached_analysis(root)
    if path is None:
        raise CacheError(
            f"No saved analysis was found for {root}. Run 'Scan & Analyze' once."
        )
    try:
        with gzip.open(path, "rb") as handle:
            payload = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise CacheError(f"The saved analysis in {path} could not be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise CacheError(f"The saved analysis in {path} is not valid.")
    if payload.get("version") != CACHE_VERSION:
        raise CacheError(
            f"The saved analysis in {path} was written by another version of "
            "UT Analyzer. Run 'Scan & Analyze' once to refresh it."
        )

    result: dict[str, object] = {"path": path, "saved_at": str(payload.get("saved_at") or "")}
    try:
        for name, model in _SECTIONS.items():
            raw = payload.get(name)
            if name in _LIST_SECTIONS:
                items = raw if isinstance(raw, list) else []
                result[name] = [_decode(item, model) for item in items]
            else:
                result[name] = _decode(raw, model) if isinstance(raw, dict) else None
    except (TypeError, ValueError) as exc:
        raise CacheError(
            f"The saved analysis in {path} could not be restored: {exc}. "
            "Run 'Scan & Analyze' once to refresh it."
        ) from exc
    if result.get("env") is None or result.get("quality") is None:
        raise CacheError(
            f"The saved analysis in {path} is incomplete. Run 'Scan & Analyze' once."
        )
    return result


# ---- generic dataclass <-> JSON -------------------------------------------


def _encode(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_encode(item) for item in value]
    return str(value)


_HINT_CACHE: dict[type, dict[str, object]] = {}


def _hints(cls: type) -> dict[str, object]:
    cached = _HINT_CACHE.get(cls)
    if cached is None:
        cached = typing.get_type_hints(cls)
        _HINT_CACHE[cls] = cached
    return cached


def _decode(value: object, hint: object):
    if hint is None or value is None:
        return None if value is None else value

    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        options = [arg for arg in typing.get_args(hint) if arg is not type(None)]
        return _decode(value, options[0]) if options else value
    if origin in (list, set):
        (item_hint,) = typing.get_args(hint) or (None,)
        items = value if isinstance(value, list) else []
        return [_decode(item, item_hint) for item in items]
    if origin is tuple:
        args = typing.get_args(hint)
        item_hint = args[0] if args else None
        items = value if isinstance(value, list) else []
        return tuple(_decode(item, item_hint) for item in items)
    if origin is dict:
        args = typing.get_args(hint)
        item_hint = args[1] if len(args) == 2 else None
        items = value if isinstance(value, dict) else {}
        return {str(key): _decode(item, item_hint) for key, item in items.items()}

    if isinstance(hint, type):
        if hint is Path:
            return Path(str(value))
        if is_dataclass(hint):
            return _decode_dataclass(value, hint)
        if hint in (str, int, float, bool) and isinstance(value, hint):
            return value
    return value


def _decode_dataclass(value: object, cls: type):
    if not isinstance(value, dict):
        return None
    hints = _hints(cls)
    kwargs: dict[str, object] = {}
    for spec in fields(cls):
        if spec.name not in value:
            continue
        decoded = _decode(value[spec.name], hints.get(spec.name))
        if decoded is None and _is_required(spec):
            continue
        kwargs[spec.name] = decoded
    return cls(**kwargs)


def _is_required(spec) -> bool:
    return spec.default is MISSING and spec.default_factory is MISSING  # type: ignore[misc]
