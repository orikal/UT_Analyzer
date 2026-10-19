"""Environment detection: picks the primary language adapter and asks it
to describe the project's build/test environment."""

from __future__ import annotations

from ...adapters.base import AdapterRegistry, LanguageAdapter
from ...models import EnvironmentInfo, ProjectConfig, ScanResult


def detect_environment(
    config: ProjectConfig,
    scan: ScanResult,
    registry: AdapterRegistry,
) -> tuple[EnvironmentInfo, LanguageAdapter | None]:
    """Detect the project environment. Returns (info, primary adapter)."""
    adapter = registry.detect_primary(config.root, scan.files)
    if adapter is None:
        info = EnvironmentInfo()
        info.notes.append(
            "Could not detect the project language. Analysis will be limited; "
            "you can still browse the report, but generation needs a detected language."
        )
        return info, None

    info = adapter.detect_environment(config.root, scan.files, config.test_dir)
    if not info.test_framework:
        info.notes.append(
            "No test framework detected. Generated tests will use the adapter default."
        )
    if not info.build_command:
        info.notes.append(
            "No build command detected; compile validation will be skipped and "
            "tests will be marked 'not validated'."
        )
    if not info.test_command:
        info.notes.append(
            "No test command detected; run validation will be skipped where a "
            "single test cannot be executed directly."
        )
    return info, adapter
