"""Filesystem helpers: safe reads, atomic writes with backup, directory walking."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Directories that are never worth scanning.
DEFAULT_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".vs",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "venv", ".venv", "env", ".tox", ".eggs",
    "build", "dist", "out", "bin", "obj", "target", "cmake-build-debug",
    "cmake-build-release", "build-coverage", ".gradle", "coverage", "htmlcov", ".next", ".nuxt",
}

MAX_FILE_SIZE = 2 * 1024 * 1024  # skip files larger than 2 MB


def is_excluded_dir(name: str) -> bool:
    return name in DEFAULT_EXCLUDED_DIRS or name.startswith(".")


def read_text_safe(path: Path, max_size: int = MAX_FILE_SIZE) -> str | None:
    """Read a text file; return None for binary/oversized/unreadable files."""
    try:
        if path.stat().st_size > max_size:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None  # probably binary
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def write_text_atomic(path: Path, content: str, backup: bool = True) -> Path | None:
    """Write text atomically. Returns the backup path if a backup was made."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".utanalyzer.bak")
        shutil.copy2(path, backup_path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".utanalyzer_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return backup_path


def remove_backup(backup_path: Path | None) -> None:
    if backup_path is not None:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass


def restore_backup(path: Path, backup_path: Path | None, existed_before: bool) -> None:
    """Restore a file from its backup (or delete it if it did not exist before)."""
    if backup_path is not None and backup_path.exists():
        shutil.copy2(backup_path, path)
        backup_path.unlink(missing_ok=True)
    elif not existed_before:
        Path(path).unlink(missing_ok=True)


def iter_subdirs(root: Path) -> list[Path]:
    """Immediate, non-excluded subdirectories of ``root``, sorted by name."""
    try:
        return sorted(
            (p for p in root.iterdir() if p.is_dir() and not is_excluded_dir(p.name)),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []
