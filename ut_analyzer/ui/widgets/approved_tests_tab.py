"""Tab listing user-approved tests with per-test apply-to-repo actions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...adapters.base import LanguageAdapter
from ...models import ApprovalState, ProposedFileChange, ProposedTest
from .compact_code_editor import configure_compact_code_editor, monospace_font


class ApprovedTestsTab(QWidget):
    """Shows approved tests; each can be written to the project individually."""

    apply_requested = Signal(object, int)  # ProposedFileChange path (Path), test id
    revert_requested = Signal(object, int)  # ProposedFileChange path (Path), test id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.summary = QLabel("Approved tests will appear here after you approve them in a file tab.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self._filters = _ApprovedTestsFilterBar()
        self._filters.changed.connect(self._apply_filters)
        self._filters.setVisible(False)
        layout.addWidget(self._filters)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self._root: Path | None = None
        self._adapter: LanguageAdapter | None = None
        self._proposals: list[ProposedFileChange] = []
        self._busy = False
        self._cards: list[_ApprovedTestCard] = []
        self._entries: list[tuple[ProposedFileChange, ProposedTest]] = []

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        for card in self._cards:
            card.set_busy(busy)

    def refresh(
        self,
        proposals: list[ProposedFileChange],
        adapter: LanguageAdapter | None,
        root: Path,
    ) -> bool:
        """Rebuild the list. Returns True if at least one approved test exists."""
        self._proposals = proposals
        self._adapter = adapter
        self._root = root
        self._clear_cards()

        approved_entries: list[tuple[ProposedFileChange, ProposedTest]] = []
        for change in proposals:
            for test in change.tests:
                if test.approval is ApprovalState.APPROVED:
                    approved_entries.append((change, test))

        if not approved_entries:
            self._entries = []
            self._filters.setVisible(False)
            self.summary.setText(
                "Approved tests will appear here after you approve them in a file tab."
            )
            return False

        self._entries = approved_entries
        self._filters.set_files(_file_keys_for_entries(approved_entries))
        self._filters.setVisible(True)

        for change, test in approved_entries:
            card = _ApprovedTestCard(change, test, root)
            card.apply_requested.connect(self._on_apply_requested)
            card.revert_requested.connect(self._on_revert_requested)
            card.set_busy(self._busy)
            self.list_layout.addWidget(card)
            self._cards.append(card)

        self.list_layout.addStretch(1)
        self._apply_filters()
        return True

    def _apply_filters(self) -> None:
        if not self._entries:
            return

        show_applied = self._filters.show_applied
        show_not_applied = self._filters.show_not_applied
        visible_files = self._filters.checked_files

        visible = 0
        for card in self._cards:
            file_key = _file_key(card.change)
            matches_status = (card.test.applied and show_applied) or (
                not card.test.applied and show_not_applied
            )
            matches_file = file_key in visible_files
            show = matches_status and matches_file
            card.setVisible(show)
            if show:
                visible += 1

        total = len(self._entries)
        pending = sum(1 for _, t in self._entries if not t.applied)
        applied = total - pending
        if visible == total:
            self.summary.setText(
                f"{total} approved test(s)"
                + (
                    f" — {applied} applied, {pending} ready to write."
                    if applied
                    else " — ready to write to the project."
                )
            )
        else:
            self.summary.setText(
                f"Showing {visible} of {total} approved test(s)"
                + (f" ({applied} applied, {pending} ready to write)" if total else "")
            )

    def _on_apply_requested(self, change: ProposedFileChange, test_id: int) -> None:
        self.apply_requested.emit(change.path, test_id)

    def _on_revert_requested(self, change: ProposedFileChange, test_id: int) -> None:
        self.revert_requested.emit(change.path, test_id)

    def _clear_cards(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards.clear()


def _file_key(change: ProposedFileChange) -> Path:
    return change.source_file if change.source_file is not None else change.path


def _file_keys_for_entries(
    entries: list[tuple[ProposedFileChange, ProposedTest]],
) -> list[tuple[Path, str]]:
    seen: set[Path] = set()
    result: list[tuple[Path, str]] = []
    for change, _ in entries:
        key = _file_key(change)
        if key in seen:
            continue
        seen.add(key)
        result.append((key, key.name))
    return sorted(result, key=lambda item: item[1].lower())


class _ApprovedTestsFilterBar(QGroupBox):
    """Checkboxes to filter approved tests by apply status and source file."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Show", parent)
        outer = QVBoxLayout(self)

        status_row = QHBoxLayout()
        status_label = QLabel("Status:")
        status_label.setStyleSheet("font-weight: bold;")
        status_row.addWidget(status_label)
        self._applied_cb = QCheckBox("Applied")
        self._applied_cb.setChecked(True)
        self._applied_cb.stateChanged.connect(self._emit_changed)
        status_row.addWidget(self._applied_cb)
        self._not_applied_cb = QCheckBox("Not applied")
        self._not_applied_cb.setChecked(True)
        self._not_applied_cb.stateChanged.connect(self._emit_changed)
        status_row.addWidget(self._not_applied_cb)
        status_row.addStretch(1)
        outer.addLayout(status_row)

        files_row = QHBoxLayout()
        files_label = QLabel("Files:")
        files_label.setStyleSheet("font-weight: bold;")
        files_row.addWidget(files_label)
        self._files_host = QWidget()
        self._files_layout = QHBoxLayout(self._files_host)
        self._files_layout.setContentsMargins(0, 0, 0, 0)
        files_row.addWidget(self._files_host, 1)
        outer.addLayout(files_row)

        self._file_checkboxes: dict[Path, QCheckBox] = {}

    @property
    def show_applied(self) -> bool:
        return self._applied_cb.isChecked()

    @property
    def show_not_applied(self) -> bool:
        return self._not_applied_cb.isChecked()

    @property
    def checked_files(self) -> set[Path]:
        return {path for path, cb in self._file_checkboxes.items() if cb.isChecked()}

    def set_files(self, files: list[tuple[Path, str]]) -> None:
        prev_checked = {path for path, cb in self._file_checkboxes.items() if cb.isChecked()}
        prev_known = set(self._file_checkboxes)

        while self._files_layout.count():
            item = self._files_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._file_checkboxes.clear()

        new_paths = {path for path, _ in files}
        for path, label in files:
            cb = QCheckBox(label)
            if path in prev_checked or path not in prev_known:
                cb.setChecked(True)
            else:
                cb.setChecked(False)
            cb.stateChanged.connect(self._emit_changed)
            self._files_layout.addWidget(cb)
            self._file_checkboxes[path] = cb
        self._files_layout.addStretch(1)

        if not new_paths.intersection(prev_checked) and prev_checked:
            for cb in self._file_checkboxes.values():
                cb.setChecked(True)

    def _emit_changed(self) -> None:
        self.changed.emit()


class _ApprovedTestCard(QGroupBox):
    apply_requested = Signal(object, int)
    revert_requested = Signal(object, int)

    def __init__(
        self,
        change: ProposedFileChange,
        test: ProposedTest,
        root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.change = change
        self.test = test
        self.setTitle(test.name)

        layout = QVBoxLayout(self)

        try:
            rel_target = change.path.relative_to(root)
        except ValueError:
            rel_target = change.path
        source_hint = ""
        if change.source_file is not None:
            try:
                rel_source = change.source_file.relative_to(root)
            except ValueError:
                rel_source = change.source_file
            source_hint = f"Source: {rel_source} → "
        meta = QLabel(f"{source_hint}Target: {rel_target}")
        meta.setWordWrap(True)
        meta.setStyleSheet("color: #555;")
        layout.addWidget(meta)

        if test.rationale or test.is_edge_case:
            rationale = QLabel(
                ("[edge case] " if test.is_edge_case else "") + test.rationale
            )
            rationale.setWordWrap(True)
            rationale.setStyleSheet("color: #666; font-style: italic;")
            layout.addWidget(rationale)

        editor = QPlainTextEdit()
        configure_compact_code_editor(
            editor,
            test.effective_code,
            monospace_font(),
            language=change.language,
        )
        layout.addWidget(editor)

        row = QHBoxLayout()
        self.status = QLabel("")
        row.addWidget(self.status)
        row.addStretch(1)
        self.apply_btn = QPushButton("Apply to code")
        self.apply_btn.clicked.connect(self._on_button_clicked)
        row.addWidget(self.apply_btn)
        layout.addLayout(row)

        self._sync_state()

    def set_busy(self, busy: bool) -> None:
        self.apply_btn.setEnabled(not busy)

    def _sync_state(self) -> None:
        if self.test.applied:
            self.status.setText("Applied")
            self.status.setStyleSheet("color: #22863a; font-weight: bold;")
            self.apply_btn.setText("Delete from code")
            self.apply_btn.setToolTip(
                "Remove this test from the project. Files created by the apply are "
                "deleted when no other test remains in them."
            )
        else:
            self.status.setText("Ready")
            self.status.setStyleSheet("color: #0366d6;")
            self.apply_btn.setText("Apply to code")
            self.apply_btn.setToolTip("Write this test to the project.")
        self._sync_frame_style()
        self.apply_btn.setEnabled(True)

    def _sync_frame_style(self) -> None:
        """Green frame + bold green title once the test is written to the project."""
        border = "#22863a" if self.test.applied else "#d0d7de"
        title = "#22863a" if self.test.applied else "#24292e"
        self.setStyleSheet(
            "QGroupBox {"
            f" border: 2px solid {border};"
            " border-radius: 6px;"
            " margin-top: 10px;"
            " padding-top: 8px;"
            "}"
            "QGroupBox::title {"
            " subcontrol-origin: margin;"
            " subcontrol-position: top left;"
            " left: 10px;"
            " padding: 0 4px;"
            " font-weight: bold;"
            f" color: {title};"
            "}"
        )

    def _on_button_clicked(self) -> None:
        if self.test.applied:
            self.revert_requested.emit(self.change, self.test.id)
        else:
            self.apply_requested.emit(self.change, self.test.id)
