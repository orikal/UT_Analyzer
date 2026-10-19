"""Project-level data models: configuration, scan results and detected environment."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectConfig:
    """User-supplied configuration for a single analysis session."""

    root: Path
    test_dir: Path | None = None
    included_dirs: list[Path] = field(default_factory=list)
    #: Folders the user marked in the tree. The scan always covers the whole
    #: project; only the suggested tests are limited to these folders.
    #: Empty means "the whole project".
    focus_dirs: list[Path] = field(default_factory=list)


@dataclass
class ScannedFile:
    """A single file discovered during the repository scan."""

    path: Path
    language: str | None = None  # adapter language id, e.g. "cpp", "python"
    is_test: bool = False
    size: int = 0


@dataclass
class ScanResult:
    """Inventory produced by the repository scanner."""

    root: Path
    files: list[ScannedFile] = field(default_factory=list)
    skipped_dirs: list[Path] = field(default_factory=list)

    def by_language(self, language: str) -> list[ScannedFile]:
        return [f for f in self.files if f.language == language]

    def source_files(self, language: str | None = None) -> list[ScannedFile]:
        return [
            f
            for f in self.files
            if not f.is_test and (language is None or f.language == language)
        ]

    def test_files(self, language: str | None = None) -> list[ScannedFile]:
        return [
            f
            for f in self.files
            if f.is_test and (language is None or f.language == language)
        ]


@dataclass
class EnvironmentInfo:
    """Automatically detected project environment.

    Every field may be ``None`` when detection was not possible; the UI shows
    "not detected" and lets the user fill values in manually.
    """

    language: str | None = None
    language_name: str | None = None
    test_framework: str | None = None
    build_system: str | None = None
    build_command: str | None = None
    test_command: str | None = None
    mocking_libraries: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary_rows(self) -> list[tuple[str, str]]:
        """Human-readable rows for the UI environment panel."""
        nd = "not detected"
        return [
            ("Language", self.language_name or self.language or nd),
            ("Test framework", self.test_framework or nd),
            ("Build system", self.build_system or nd),
            ("Mocking libraries", ", ".join(self.mocking_libraries) or nd),
        ]
