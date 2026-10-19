"""Helpers for gathering test file contents during environment detection."""

from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path

from ...models import ScannedFile
from ...utils.fs import is_excluded_dir, read_text_safe

_TEST_DIR_NAMES = frozenset(
    {
        "test",
        "tests",
        "ut",
        "uts",
        "unittest",
        "unittests",
        "spec",
        "specs",
        "testing",
        "__tests__",
    }
)

_PREFERRED_RELATIVE_PATHS = frozenset(
    {
        "tests",
        "test",
        "ut",
        "src/test",
        "src/tests",
        "src/test/java",
        "src/test/kotlin",
        "__tests__",
    }
)

_TEST_PATH_RE = re.compile(
    r"(^test_|_test$|_tests$|^tests?$|Test[A-Z_]|Tests?\b)",
    re.IGNORECASE,
)

# Folder names looked up directly under the repository root, in priority order.
_ROOT_PRIORITY_DIR_NAMES: tuple[str, ...] = ("unittest", "ut")

#: The unit-test folder this tool expects, relative to the repository root:
#: ``<root>/UnitTests/Code/tests``. It wins over every other candidate, and new
#: tests are written there (the folders are created on the first write).
PREFERRED_TEST_PATH: tuple[str, ...] = ("UnitTests", "Code", "tests")


def is_test_directory(path: Path) -> bool:
    """Whether the selected folder is itself a unit-test directory."""
    name = path.name.lower()
    if name in _TEST_DIR_NAMES:
        return True
    return "test" in name or name.endswith("_ut")


def _child_named(parent: Path, name: str) -> Path | None:
    """Case-insensitive lookup of a direct sub-directory of *parent*."""
    direct = parent / name
    if direct.is_dir():
        return direct
    lowered = name.lower()
    try:
        for child in parent.iterdir():
            if child.is_dir() and child.name.lower() == lowered:
                return child
    except OSError:
        return None
    return None


def preferred_test_directory(root: Path) -> Path:
    """``<root>/UnitTests/Code/tests``, whether or not it exists yet.

    Folders that already exist keep the casing they have on disk.
    """
    current = root
    for name in PREFERRED_TEST_PATH:
        current = _child_named(current, name) or current / name
    return current


def find_preferred_test_directory(root: Path) -> Path | None:
    """The preferred unit-test folder, only when it already exists on disk."""
    candidate = preferred_test_directory(root)
    return candidate if candidate.is_dir() else None


def _find_root_priority_dir(root: Path) -> Path | None:
    """Look for a preferred UT folder directly under *root* (no recursion)."""
    try:
        children = {
            child.name.lower(): child
            for child in root.iterdir()
            if child.is_dir() and not is_excluded_dir(child.name)
        }
    except OSError:
        return None
    for name in _ROOT_PRIORITY_DIR_NAMES:
        match = children.get(name)
        if match is not None:
            return match
    return None


def _relative_path_key(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix().lower()
    except ValueError:
        return None


def _is_test_directory_candidate(path: Path, root: Path) -> bool:
    if is_test_directory(path):
        return True
    rel = _relative_path_key(path, root)
    return rel is not None and rel in _PREFERRED_RELATIVE_PATHS


def _score_test_directory(path: Path, root: Path, depth: int, max_depth: int) -> int:
    name = path.name.lower()
    score = 0
    if name in _TEST_DIR_NAMES:
        score += 20
    if "test" in name or name.endswith("_ut"):
        score += 10
    score += 5 * (max_depth - depth)
    rel = _relative_path_key(path, root)
    if rel is not None and rel in _PREFERRED_RELATIVE_PATHS:
        score += 15
    return score


def detect_test_directory(root: Path, *, max_depth: int = 6) -> Path | None:
    """Find the most likely unit-test directory under a repository root."""
    resolved = root.resolve()
    if not resolved.is_dir():
        return None

    preferred = find_preferred_test_directory(resolved)
    if preferred is not None:
        return preferred

    if is_test_directory(resolved):
        return resolved

    priority = _find_root_priority_dir(resolved)
    if priority is not None:
        return priority

    best: Path | None = None
    best_score = -1
    candidates: list[tuple[Path, int]] = []
    queue: deque[tuple[Path, int]] = deque([(resolved, 0)])

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or is_excluded_dir(child.name):
                continue
            child_depth = depth + 1
            if _is_test_directory_candidate(child, resolved):
                score = _score_test_directory(child, resolved, child_depth, max_depth)
                candidates.append((child, score))
            if child_depth < max_depth:
                queue.append((child, child_depth))

    if not candidates:
        return None

    candidate_paths = [path for path, _ in candidates]
    filtered = [
        (path, score)
        for path, score in candidates
        if not any(
            path != other and other.is_relative_to(path) for other in candidate_paths
        )
    ]
    for path, score in filtered:
        if score > best_score:
            best_score = score
            best = path

    return best


def is_test_like_path(path: Path) -> bool:
    """Whether a path looks test-related even when ``is_test`` was not set."""
    lowered = [part.lower() for part in path.parts]
    if any(part in _TEST_DIR_NAMES or "test" in part or "spec" in part for part in lowered):
        return True
    return bool(_TEST_PATH_RE.search(path.stem))


def _path_key(path: Path) -> str:
    """Comparable, case-insensitive absolute path string (no filesystem access)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _directory_keys(directory: Path) -> tuple[str, ...]:
    """Both the plain and the fully resolved key of *directory*."""
    keys = [_path_key(directory)]
    try:
        resolved = _path_key(directory.resolve())
    except OSError:
        resolved = keys[0]
    if resolved != keys[0]:
        keys.append(resolved)
    return tuple(keys)


def _is_within(path_key: str, directory_keys: tuple[str, ...]) -> bool:
    return any(
        path_key == key or path_key.startswith(key + os.sep) for key in directory_keys
    )


def resolve_test_directory(
    root: Path | None, test_dir: Path | None = None
) -> Path | None:
    """The project's unit-test folder: the configured one, else auto-detected."""
    if test_dir is not None and test_dir.is_dir():
        return test_dir
    if root is None:
        return None
    return detect_test_directory(root)


def planned_test_directory(root: Path, test_dir: Path | None = None) -> Path:
    """Where unit tests belong, even when no test folder exists yet.

    Falls back to ``<root>/UnitTests/Code/tests``; the folders are created by the
    first write, never by analysis.
    """
    existing = resolve_test_directory(root, test_dir)
    if existing is not None:
        return existing
    return preferred_test_directory(root)


def sample_test_contents(
    files: list[ScannedFile],
    language_id: str,
    *,
    scan_root: Path | None = None,
    test_dir: Path | None = None,
    limit: int = 40,
) -> list[str]:
    """Read existing unit-test sources to detect the test framework.

    Files living in the project's unit-test folder are read first: a scan of the
    whole repository must detect the same framework as a scan limited to that
    folder, and test-looking file names elsewhere must not use up the budget.
    """
    contents: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> bool:
        """Read *path* into the sample. Returns True once the limit is reached."""
        if path not in seen:
            seen.add(path)
            text = read_text_safe(path)
            if text:
                contents.append(text)
        return len(contents) >= limit

    candidates = [scanned for scanned in files if scanned.language == language_id]

    if scan_root is not None and is_test_directory(scan_root):
        for scanned in candidates:
            if add(scanned.path):
                break
        return contents

    ut_dir = resolve_test_directory(scan_root, test_dir)
    ut_keys = _directory_keys(ut_dir) if ut_dir is not None else ()

    in_ut_dir: list[Path] = []
    marked_tests: list[Path] = []
    test_like: list[Path] = []
    for scanned in candidates:
        if ut_keys and _is_within(_path_key(scanned.path), ut_keys):
            in_ut_dir.append(scanned.path)
        elif scanned.is_test:
            marked_tests.append(scanned.path)
        elif is_test_like_path(scanned.path):
            test_like.append(scanned.path)

    for tier in (in_ut_dir, marked_tests, test_like):
        for path in tier:
            if add(path):
                return contents

    return contents
