"""Application path resolution for development and frozen (PyInstaller) builds."""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """Read-only bundle directory (PyInstaller _MEIPASS or project root in dev)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def install_root() -> Path:
    """Directory containing the main executable."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def program_data_root() -> Path:
    """Writable per-user application data directory."""
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        return Path(local_app) / "UT Analyzer"
    return Path.home() / "AppData" / "Local" / "UT Analyzer"


def config_dir() -> Path:
    path = program_data_root() / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = program_data_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = program_data_root() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_dir() -> Path:
    path = program_data_root() / "temporary"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_licenses_dir() -> Path:
    if is_frozen():
        return bundle_root() / "licenses"
    return install_root() / "licenses"


def bundled_default_config_dir() -> Path:
    if is_frozen():
        return bundle_root() / "default-config"
    return install_root() / "default-config"


def release_config_path() -> Path:
    candidates = [
        install_root() / "default-config" / "versions.json",
        bundle_root() / "default-config" / "versions.json",
        bundle_root() / "release" / "versions.json",
        Path(__file__).resolve().parents[2] / "release" / "versions.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


@lru_cache(maxsize=1)
def load_release_config() -> dict:
    path = release_config_path()
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)
