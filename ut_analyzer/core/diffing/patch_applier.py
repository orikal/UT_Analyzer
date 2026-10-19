"""Applies approved test changes to disk, atomically and with backups."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...adapters.base import LanguageAdapter
from ...models import ApprovalState, ProposedFileChange, ProposedTest
from ...utils.fs import remove_backup, write_text_atomic
from .diff_engine import compose_content


@dataclass
class ApplyOutcome:
    written: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    errors: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class RevertOutcome:
    """Result of removing a previously applied test from the project."""

    updated: list[Path] = field(default_factory=list)  # files rewritten without the test
    deleted: list[Path] = field(default_factory=list)  # files removed entirely
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    errors: list[tuple[Path, str]] = field(default_factory=list)


def _remember_created(change: ProposedFileChange, path: Path) -> None:
    """Record that this tool created ``path`` so a revert may remove it again."""
    if path not in change.created_paths:
        change.created_paths.append(path)


def apply_approved_changes(
    changes: list[ProposedFileChange],
    adapter: LanguageAdapter,
    root: Path,
) -> ApplyOutcome:
    """Write only approved tests. Rejected/pending tests never touch the disk."""
    outcome = ApplyOutcome()
    for change in changes:
        approved = change.approved_tests
        if not approved:
            outcome.skipped.append((change.path, "no approved tests"))
            continue
        if not _is_within(change.path, root):
            outcome.errors.append(
                (change.path, "target is outside the selected project directory")
            )
            continue
        content = compose_content(change, adapter, only_approved=True)
        try:
            _write_companion_header(change, root, outcome)
            created = not change.path.is_file()
            backup = write_text_atomic(change.path, content, backup=True)
            remove_backup(backup)  # apply succeeded; the backup is no longer needed
            if created:
                _remember_created(change, change.path)
            for test in approved:
                if not test.applied_block:
                    test.applied_block = test.effective_code.rstrip()
                test.applied = True
            outcome.written.append(change.path)
        except OSError as exc:
            outcome.errors.append((change.path, str(exc)))
    return outcome


def _write_companion_header(
    change: ProposedFileChange,
    root: Path,
    outcome: ApplyOutcome,
) -> None:
    """Create the UT header next to the test file (never overwrites an existing one)."""
    header = change.header_path
    if header is None or not change.header_content:
        return
    if header.is_file():
        return
    if not _is_within(header, root):
        outcome.errors.append((header, "target is outside the selected project directory"))
        return
    backup = write_text_atomic(header, change.header_content, backup=True)
    remove_backup(backup)
    _remember_created(change, header)
    outcome.written.append(header)


def _read_base_content(change: ProposedFileChange) -> str:
    if change.path.is_file():
        return change.path.read_text(encoding="utf-8", errors="replace")
    if change.is_new_file and change.preamble:
        return change.preamble.rstrip("\n") + "\n"
    return change.original_content


def apply_single_approved_test(
    change: ProposedFileChange,
    test: ProposedTest,
    adapter: LanguageAdapter,
    root: Path,
) -> ApplyOutcome:
    """Append one approved test to the target file on disk."""
    outcome = ApplyOutcome()
    if test.approval is not ApprovalState.APPROVED:
        outcome.skipped.append((change.path, "test is not approved"))
        return outcome
    if test.applied:
        outcome.skipped.append((change.path, "test already applied"))
        return outcome
    if not _is_within(change.path, root):
        outcome.errors.append(
            (change.path, "target is outside the selected project directory")
        )
        return outcome

    block = test.effective_code.rstrip()
    if not block:
        outcome.skipped.append((change.path, "test has no code"))
        return outcome

    base = _read_base_content(change)
    insert_at = adapter.insertion_point(base)
    before = base[:insert_at].rstrip("\n")
    after = base[insert_at:]
    content = before + "\n\n" + block + "\n" + after

    try:
        _write_companion_header(change, root, outcome)
        created = not change.path.is_file()
        backup = write_text_atomic(change.path, content, backup=True)
        remove_backup(backup)
        test.applied = True
        test.applied_block = block
        if created:
            _remember_created(change, change.path)
        if change.is_new_file and not change.original_content:
            change.original_content = content
        outcome.written.append(change.path)
    except OSError as exc:
        outcome.errors.append((change.path, str(exc)))
    return outcome


def revert_single_applied_test(
    change: ProposedFileChange,
    test: ProposedTest,
    adapter: LanguageAdapter,
    root: Path,
) -> RevertOutcome:
    """Remove a previously applied test from the project.

    When the test was the last one left in the target file, every file this tool
    created for it (the test file and its companion header) is deleted. Otherwise
    only the test block itself is stripped and the file is rewritten.
    """
    outcome = RevertOutcome()
    if not test.applied:
        outcome.skipped.append((change.path, "test is not applied"))
        return outcome
    if not _is_within(change.path, root):
        outcome.errors.append(
            (change.path, "target is outside the selected project directory")
        )
        return outcome
    if not change.path.is_file():
        test.applied = False
        test.applied_block = ""
        outcome.skipped.append((change.path, "file no longer exists"))
        return outcome

    try:
        content = change.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        outcome.errors.append((change.path, str(exc)))
        return outcome

    stripped = _strip_test_block(content, test, adapter, change.path)
    if stripped is None:
        outcome.skipped.append(
            (change.path, "the test code was not found in the file (edited manually?)")
        )
        return outcome

    remaining = len(adapter.parse_test_file(change.path, stripped).cases)
    try:
        if remaining == 0 and change.path in change.created_paths:
            _delete_created_files(change, root, outcome)
            change.original_content = ""
            for other in change.tests:
                other.applied = False
                other.applied_block = ""
        else:
            backup = write_text_atomic(change.path, stripped, backup=True)
            remove_backup(backup)
            if change.is_new_file:
                change.original_content = stripped
            outcome.updated.append(change.path)
    except OSError as exc:
        outcome.errors.append((change.path, str(exc)))
        return outcome

    test.applied = False
    test.applied_block = ""
    return outcome


def _delete_created_files(
    change: ProposedFileChange,
    root: Path,
    outcome: RevertOutcome,
) -> None:
    """Delete every file this tool created for ``change`` (test file + header)."""
    for path in list(change.created_paths):
        if not _is_within(path, root):
            outcome.errors.append(
                (path, "target is outside the selected project directory")
            )
            continue
        if path.is_file():
            path.unlink()
            outcome.deleted.append(path)
        change.created_paths.remove(path)


def _strip_test_block(
    content: str,
    test: ProposedTest,
    adapter: LanguageAdapter,
    path: Path,
) -> str | None:
    """Return ``content`` without the test's code, or None when it cannot be located."""
    block = (test.applied_block or test.effective_code).rstrip()
    start = content.find(block) if block else -1
    if start < 0:
        start, block = _locate_by_parsing(content, test, adapter, path)
        if start < 0:
            return None

    head = content[:start].rstrip("\n")
    tail = content[start + len(block) :].lstrip("\n")
    if head and tail:
        return head + "\n\n" + tail
    if head:
        return head + "\n"
    return tail


def _locate_by_parsing(
    content: str,
    test: ProposedTest,
    adapter: LanguageAdapter,
    path: Path,
) -> tuple[int, str]:
    """Fallback lookup: find the test by name using the adapter's parser."""
    for case in adapter.parse_test_file(path, content).cases:
        if case.name != test.name or not case.body:
            continue
        body_at = content.find(case.body)
        if body_at < 0:
            continue
        # Walk back to the start of the line declaring the test.
        line_start = content.rfind("\n", 0, body_at) + 1
        decl_start = content.rfind("\n", 0, line_start - 1) + 1 if line_start else 0
        start = line_start if content[line_start:body_at].strip() else decl_start
        return start, content[start : body_at + len(case.body)]
    return -1, ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
