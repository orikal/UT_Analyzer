"""Repository scanner: walks selected folders and builds the file inventory."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from ...adapters.base import AdapterRegistry
from ...core.cancellation import raise_if_cancelled
from ...models import ProjectConfig, ScannedFile, ScanResult
from ...utils.fs import MAX_FILE_SIZE, is_excluded_dir, read_text_safe

MAX_FILES = 20000

#: Seconds between progress messages. A repository can hold tens of thousands
#: of folders, and one UI update per folder buries the event loop so deeply
#: that the window stops reacting to clicks.
_PROGRESS_INTERVAL = 0.2


def scan_repository(
    config: ProjectConfig,
    registry: AdapterRegistry,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ScanResult:
    """Walk the included directories and classify every relevant file.

    Test classification: fast name-based check first; content check only for
    files whose name is ambiguous, to keep large scans quick.
    """
    result = ScanResult(root=config.root)
    ext_map = registry.extension_map()
    roots = list(config.included_dirs or [config.root])
    if config.test_dir is not None and config.test_dir.is_dir():
        test_dir = config.test_dir.resolve()
        if not any(test_dir == root.resolve() or test_dir.is_relative_to(root.resolve()) for root in roots):
            roots.append(test_dir)
    seen: set[Path] = set()
    last_report = 0.0

    for scan_root in roots:
        raise_if_cancelled(cancelled)
        for dirpath, dirnames, filenames in os.walk(scan_root):
            raise_if_cancelled(cancelled)
            current = Path(dirpath)
            # prune excluded and already-visited directories in place
            dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
            if current in seen:
                dirnames[:] = []
                continue
            seen.add(current)
            if progress is not None:
                now = time.monotonic()
                if now - last_report >= _PROGRESS_INTERVAL:
                    last_report = now
                    progress(f"Scanning ({len(result.files)} files): {current}")

            for filename in filenames:
                if len(result.files) >= MAX_FILES:
                    result.skipped_dirs.append(current)
                    return result
                path = current / filename
                language = ext_map.get(path.suffix.lower())
                if language is None:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_SIZE:
                    continue

                adapter = registry.get(language)
                is_test = False
                if adapter is not None:
                    is_test = adapter.is_test_file(path)
                    if not is_test and _needs_content_check(path):
                        content = read_text_safe(path)
                        if content is not None:
                            is_test = adapter.is_test_file(path, content)
                result.files.append(
                    ScannedFile(path=path, language=language, is_test=is_test, size=size)
                )
    return result


def _needs_content_check(path: Path) -> bool:
    """Read file content when the path hints at tests or uses common test naming."""
    lowered = [p.lower() for p in path.parts]
    if any("test" in part or "spec" in part for part in lowered):
        return True
    if path.suffix.lower() in (".cpp", ".cc", ".cxx", ".cs", ".java", ".py"):
        stem = path.stem
        return bool(
            re.search(r"(^test_|_test$|_tests$|Test$|Tests$)", stem, re.IGNORECASE)
        )
    if path.suffix.lower() in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return bool(re.search(r"\.(test|spec)\.", path.name, re.IGNORECASE))
    return False
