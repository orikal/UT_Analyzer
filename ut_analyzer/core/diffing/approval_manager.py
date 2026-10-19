"""Approval state management for proposed test changes."""

from __future__ import annotations

from ...models import ApprovalState, ProposedFileChange, ProposedTest


class ApprovalManager:
    """Tracks per-test and per-file approve/reject/edit state."""

    def __init__(self) -> None:
        self.changes: list[ProposedFileChange] = []

    def set_changes(self, changes: list[ProposedFileChange]) -> None:
        self.changes = changes

    # ---- per-test operations ------------------------------------------------

    def approve_test(self, test: ProposedTest) -> None:
        test.approval = ApprovalState.APPROVED

    def reject_test(self, test: ProposedTest) -> None:
        test.approval = ApprovalState.REJECTED

    def reset_test(self, test: ProposedTest) -> None:
        test.approval = ApprovalState.PENDING

    def edit_test(self, test: ProposedTest, new_code: str) -> None:
        test.edited_code = new_code if new_code != test.code else None

    # ---- per-file operations --------------------------------------------------

    def approve_file(self, change: ProposedFileChange) -> None:
        change.approve_all()

    def reject_file(self, change: ProposedFileChange) -> None:
        change.reject_all()

    # ---- aggregate statistics ---------------------------------------------------

    def stats(self) -> dict[str, int]:
        tests = [t for c in self.changes for t in c.tests]
        return {
            "generated": len(tests),
            "approved": sum(1 for t in tests if t.approval is ApprovalState.APPROVED),
            "rejected": sum(1 for t in tests if t.approval is ApprovalState.REJECTED),
            "pending": sum(1 for t in tests if t.approval is ApprovalState.PENDING),
            "edge_cases": sum(1 for t in tests if t.is_edge_case),
        }

    def files_with_approvals(self) -> list[ProposedFileChange]:
        return [c for c in self.changes if c.approved_tests]
