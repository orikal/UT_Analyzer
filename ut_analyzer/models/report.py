"""Report models: quality analysis and before/after comparison."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

# Gap kinds -----------------------------------------------------------------
GAP_NO_TEST_FILE = "no_test_file"          # source file has no unit-test file at all
GAP_UNTESTED_FUNCTIONS = "untested_functions"  # test file exists, some functions untested
GAP_ADDITIONAL_CASES = "additional_cases"  # test file exists and covers the API: add more cases

_GAP_LABELS = {
    GAP_NO_TEST_FILE: "No unit tests",
    GAP_UNTESTED_FUNCTIONS: "Partially tested",
    GAP_ADDITIONAL_CASES: "Tested - add cases",
}


def _notes_for(notes: dict[str, str], names: Iterable[str]) -> dict[str, str]:
    return {name: notes[name] for name in names if name in notes}


@dataclass
class QualityGap:
    """A concrete testing gap the generator should try to fill."""

    source_file: Path
    reason: str                             # e.g. "no test file", "functions untested"
    untested_functions: list[str] = field(default_factory=list)
    uncertain_functions: list[str] = field(default_factory=list)
    coverage_notes: dict[str, str] = field(default_factory=dict)
    existing_test_file: Path | None = None
    kind: str = GAP_NO_TEST_FILE
    existing_test_count: int = 0            # test cases already present in the test file
    ambiguous_test_file: bool = False
    test_file_alternatives: list[Path] = field(default_factory=list)
    all_functions: list[str] = field(default_factory=list)      # every callable declared
    function_states: dict[str, str] = field(default_factory=dict)  # name -> coverage state
    non_public_functions: list[str] = field(default_factory=list)  # private/protected
    focus_only: bool = False                # generate tests ONLY for untested_functions

    @property
    def has_existing_tests(self) -> bool:
        """True when tests already exist and new cases should be appended to them."""
        return self.kind != GAP_NO_TEST_FILE

    def status_label(self) -> str:
        """Short human-readable coverage status for the UI."""
        return _GAP_LABELS.get(self.kind, self.kind)

    def selectable_functions(self) -> list[str]:
        """Every symbol of this file the user can ask tests for, in a stable order."""
        names: list[str] = []
        seen: set[str] = set()
        for name in (
            *self.all_functions,
            *self.untested_functions,
            *self.uncertain_functions,
        ):
            if name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def default_selection(self) -> list[str]:
        """Symbols pre-ticked in the UI: the ones without confident coverage."""
        names = self.selectable_functions()
        missing = [n for n in names if self.coverage_state(n) != "covered"]
        return missing or names

    def coverage_state(self, name: str) -> str:
        """``"untested"`` / ``"uncertain"`` / ``"covered"`` for one symbol."""
        state = self.function_states.get(name)
        if state:
            return state
        if name in self.uncertain_functions:
            return "uncertain"
        if name in self.untested_functions:
            return "untested"
        return "untested"

    def is_non_public(self, name: str) -> bool:
        return name in self.non_public_functions

    def focused_on(self, names: Sequence[str]) -> QualityGap:
        """A copy of this gap whose generation is restricted to ``names``."""
        wanted = set(names)
        chosen = [n for n in self.selectable_functions() if n in wanted]
        if not chosen:
            return self
        return replace(
            self,
            untested_functions=chosen,
            uncertain_functions=[n for n in self.uncertain_functions if n in wanted],
            coverage_notes=_notes_for(self.coverage_notes, chosen),
            focus_only=True,
        )


@dataclass
class QualityReport:
    """Status report about the current unit-test quality."""

    total_source_files: int = 0
    total_test_files: int = 0
    total_test_cases: int = 0
    tests_without_assertions: int = 0
    avg_assertions_per_test: float = 0.0
    trivial_tests: list[str] = field(default_factory=list)     # "file::test"
    files_without_tests: list[str] = field(default_factory=list)
    files_with_tests: list[str] = field(default_factory=list)  # already tested, can get more cases
    gaps: list[QualityGap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unparsed_files: list[str] = field(default_factory=list)


@dataclass
class ProjectSnapshot:
    """Metrics captured at a point in time, for before/after comparison."""

    test_file_count: int = 0
    test_case_count: int = 0
    files_without_tests: list[str] = field(default_factory=list)


@dataclass
class BeforeAfterReport:
    before: ProjectSnapshot = field(default_factory=ProjectSnapshot)
    after: ProjectSnapshot = field(default_factory=ProjectSnapshot)
    generated_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    edge_case_count: int = 0
    compiled_count: int = 0
    passed_count: int = 0
    failed_or_unvalidated: list[tuple[str, str]] = field(default_factory=list)  # (test, reason)
