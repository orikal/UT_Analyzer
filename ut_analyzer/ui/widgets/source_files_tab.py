"""Tab listing source files with testing gaps and per-file apply actions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from ...adapters.base import LanguageAdapter
from ...core.generation.stream_sink import CHANNEL_CONTENT, CHANNEL_REASONING
from ...models import GAP_NO_TEST_FILE, ProposedFileChange, QualityGap, QualityReport
from .compact_code_editor import configure_compact_code_editor, monospace_font

_COL_SOURCE = 0
_COL_FILE_NAME = 1
_COL_EXISTING_UT = 2
_COL_ACTION = 3
_GENERATING_LABEL = "Generating...."
_GENERATED_LABEL = "Generated."
_NEW_TESTS_LABEL = "Press To Generate"
_MORE_TESTS_LABEL = "Add Test Cases"
_CHOOSE_PATH_LABEL = "..."
_REGENERATE_LABEL = "\u21bb"  # clockwise open circle arrow
_REGENERATE_TOOLTIP = "Generate new tests for this file again"

# Live model output: keep the tail only, so a long run cannot grow without bound.
_MAX_STREAM_CHARS = 200_000
_STREAM_HEADINGS = {
    CHANNEL_REASONING: "----- model thinking -----",
    CHANNEL_CONTENT: "----- model writing tests -----",
}

_FUNCTION_ROLE = Qt.ItemDataRole.UserRole
_COVERAGE_TEXT = {
    "untested": "no test calls it",
    "uncertain": "coverage uncertain",
    "covered": "already covered",
}
_COVERAGE_COLOR = {
    "untested": "#b31d28",
    "uncertain": "#b08800",
    "covered": "#22863a",
}


def _table_item(
    text: str,
    *,
    tooltip: str = "",
    center: bool = False,
) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if center:
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    else:
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
    if tooltip:
        item.setToolTip(tooltip)
    return item


class _SourceFilePreviewPanel(QWidget):
    """Read-only preview of proposed tests for the selected source file."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)

        self.header = QLabel("Select a source file to preview proposed tests.")
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #666;")
        layout.addWidget(self.hint)

        self.tests_scroll = QScrollArea()
        self.tests_scroll.setWidgetResizable(True)
        self.tests_container = QWidget()
        self.tests_layout = QVBoxLayout(self.tests_container)
        self.tests_layout.setContentsMargins(0, 0, 0, 0)
        self.tests_scroll.setWidget(self.tests_container)
        self.tests_scroll.setVisible(False)
        self._body = self.tests_scroll
        layout.addWidget(self.tests_scroll, 1)

        self.live_view = QPlainTextEdit()
        self.live_view.setReadOnly(True)
        self.live_view.setFont(monospace_font())
        self.live_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.live_view.setPlaceholderText(
            "Waiting for the model to start working on this file..."
        )
        self.live_view.setVisible(False)
        layout.addWidget(self.live_view, 1)

        self._test_editors: list[QPlainTextEdit] = []

    def clear(self) -> None:
        self.header.setText("Select a source file to preview proposed tests.")
        self.hint.setText("")
        self._body.setVisible(False)
        self.live_view.setVisible(False)
        self._clear_test_editors()

    def set_live_text(self, text: str) -> None:
        self.live_view.setPlainText(text)
        self._scroll_live_to_end()

    def append_live_text(self, text: str) -> None:
        cursor = self.live_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._scroll_live_to_end()

    def _scroll_live_to_end(self) -> None:
        scroll_bar = self.live_view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def show_gap_details(self, gap: QualityGap, *, status: str) -> None:
        self._body.setVisible(False)
        self._clear_test_editors()

        if status == "generating":
            self.header.setText(f"<b>{gap.source_file.name}</b> — the model is working…")
            self.hint.setText(
                "Live model output: its reasoning and the tests it is writing appear "
                "below until the finished tests replace them."
            )
            self.live_view.setVisible(True)
            return

        self.live_view.setVisible(False)

        untested = ", ".join(gap.untested_functions) if gap.untested_functions else "—"
        self.header.setText(f"<b>{gap.source_file.name}</b>")
        if gap.has_existing_tests:
            existing = (
                f"{gap.existing_test_count} existing test(s)"
                if gap.existing_test_count
                else "an existing test file"
            )
            self.hint.setText(
                f"This file already has {existing}.\n"
                f"Untested functions: {untested}\n\n"
                "Click Add Test Cases to propose additional tests that will be appended "
                "to the existing test file."
            )
            return
        self.hint.setText(
            "Untested functions: "
            f"{untested}\n\n"
            "Click Press To Generate to create unit tests for this file."
        )

    def show_proposal(
        self,
        gap: QualityGap,
        change: ProposedFileChange,
        adapter: LanguageAdapter,
        root: Path,
    ) -> None:
        self._clear_test_editors()
        self.hint.setText("")
        self.live_view.setVisible(False)

        try:
            rel_target = change.path.relative_to(root)
        except ValueError:
            rel_target = change.path
        new_file = " (new file)" if change.is_new_file else ""
        suffix = " to append" if not change.is_new_file else ""
        self.header.setText(
            f"<b>{gap.source_file.name}</b> → "
            f"<span style='color:#0366d6'>{rel_target}</span>{new_file} "
            f"— {len(change.tests)} proposed test(s){suffix}"
        )

        self._body.setVisible(True)

        mono = monospace_font()

        for test in change.tests:
            title = QLabel(f"<b>{test.name}</b>")
            if test.is_edge_case:
                title.setText(f"<b>{test.name}</b> <span style='color:#b08800'>[edge case]</span>")
            self.tests_layout.addWidget(title)

            if test.rationale:
                rationale = QLabel(test.rationale)
                rationale.setWordWrap(True)
                rationale.setStyleSheet("color: #555; font-style: italic; margin-bottom: 4px;")
                self.tests_layout.addWidget(rationale)

            editor = QPlainTextEdit()
            configure_compact_code_editor(
                editor,
                test.effective_code,
                mono,
                language=change.language or adapter.language_id,
            )
            self.tests_layout.addWidget(editor)
            self._test_editors.append(editor)

        self.tests_layout.addStretch(1)

    def _clear_test_editors(self) -> None:
        while self.tests_layout.count():
            item = self.tests_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._test_editors.clear()


class _FunctionSelectionPanel(QWidget):
    """Checkable list of the symbols of the selected source file."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 8, 0)

        self.header = QLabel("Select a source file to choose functions.")
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        self.all_button = QPushButton("Select all")
        self.untested_button = QPushButton("Only untested")
        self.none_button = QPushButton("Clear")
        for button in (self.all_button, self.untested_button, self.none_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.all_button.clicked.connect(lambda: self._check_all(True))
        self.none_button.clicked.connect(lambda: self._check_all(False))
        self.untested_button.clicked.connect(self._check_untested)

        self._gap: QualityGap | None = None
        self._loading = False

    def clear(self) -> None:
        self._gap = None
        self._loading = True
        self.list.clear()
        self._loading = False
        self.header.setText("Select a source file to choose functions.")
        self._set_buttons_enabled(False)

    def show_gap(self, gap: QualityGap, selected: set[str]) -> None:
        self._gap = gap
        self._loading = True
        self.list.clear()
        names = gap.selectable_functions()
        for name in names:
            state = gap.coverage_state(name)
            item = QListWidgetItem(f"{name}  —  {_COVERAGE_TEXT[state]}")
            item.setData(_FUNCTION_ROLE, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked
            )
            item.setForeground(QBrush(QColor(_COVERAGE_COLOR[state])))
            note = gap.coverage_notes.get(name, "")
            item.setToolTip(note or name)
            self.list.addItem(item)
        self._loading = False

        if names:
            self.header.setText(
                f"Functions of <b>{gap.source_file.name}</b> — tick the ones you want "
                "tests for, then click the button on that row."
            )
        else:
            self.header.setText(
                f"No testable symbols were parsed in <b>{gap.source_file.name}</b>; "
                "tests will be generated for the whole file."
            )
        self._set_buttons_enabled(bool(names))

    def checked_functions(self) -> list[str]:
        names: list[str] = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                names.append(str(item.data(_FUNCTION_ROLE)))
        return names

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self.all_button.setEnabled(enabled)
        self.none_button.setEnabled(enabled)
        self.untested_button.setEnabled(enabled and self._gap is not None)

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._loading = True
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)
        self._loading = False
        self.selection_changed.emit()

    def _check_untested(self) -> None:
        if self._gap is None:
            return
        wanted = set(self._gap.untested_functions) | set(self._gap.uncertain_functions)
        self._loading = True
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(_FUNCTION_ROLE)) in wanted
                else Qt.CheckState.Unchecked
            )
        self._loading = False
        self.selection_changed.emit()

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        if self._loading:
            return
        self.selection_changed.emit()


class SourceFilesTab(QWidget):
    """Shows quality gaps; row click loads preview, Apply writes tests to the repo."""

    preview_requested = Signal(object, object, object)  # source file, symbols, target UT path
    regenerate_requested = Signal(object, object, object)  # same payload, forces a new run
    apply_requested = Signal(object)  # Path (source file)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.summary = QLabel("Run 'Scan & Analyze' to list source files with testing gaps.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(6)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Source File", "File Name", "Existing UT", "Action"]
        )
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(24)
        header.setSectionResizeMode(_COL_SOURCE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_FILE_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_EXISTING_UT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeMode.ResizeToContents)

        vheader = self.table.verticalHeader()
        vheader.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        vheader.setMinimumSectionSize(28)
        vheader.setDefaultSectionSize(32)
        vheader.setVisible(True)

        self.table.setWordWrap(True)
        self.table.setHorizontalScrollMode(
            self.table.ScrollMode.ScrollPerPixel
        )
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.table)

        bottom = QSplitter(Qt.Orientation.Horizontal)
        bottom.setChildrenCollapsible(True)
        self.functions = _FunctionSelectionPanel()
        self.functions.selection_changed.connect(self._on_functions_changed)
        bottom.addWidget(self.functions)

        self.preview = _SourceFilePreviewPanel()
        bottom.addWidget(self.preview)
        bottom.setStretchFactor(0, 1)
        bottom.setStretchFactor(1, 2)
        bottom.setSizes([260, 520])

        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 280])
        layout.addWidget(splitter, 1)

        self._root: Path | None = None
        self._gaps: list[QualityGap] = []
        self._generation_status: dict[Path, str] = {}
        self._proposals: list[ProposedFileChange] = []
        self._adapter: LanguageAdapter | None = None
        self._ai_ready = False
        self._ai_reason = ""
        self._busy = False
        self._suppress_selection_events = False
        self._stream_text: dict[Path, str] = {}
        self._stream_channel: dict[Path, str] = {}
        self._selected_functions: dict[Path, list[str]] = {}
        self._target_overrides: dict[Path, Path] = {}

    def set_gaps(
        self,
        quality: QualityReport,
        root: Path,
        *,
        generation_status: dict[Path, str] | None = None,
    ) -> None:
        previous = self._selected_gap()
        keep_source = previous.source_file.resolve() if previous is not None else None
        self._root = root
        self._gaps = list(quality.gaps)
        self._generation_status = dict(generation_status or {})
        self._stream_text.clear()
        self._stream_channel.clear()
        self._keep_user_choices()
        self._rebuild_table(keep_source=keep_source)
        count = len(self._gaps)
        if count:
            missing = sum(1 for g in self._gaps if not g.has_existing_tests)
            existing = count - missing
            self.summary.setText(
                f"{count} source file(s) listed: {missing} without unit tests, "
                f"{existing} with existing unit tests that can get more test cases. "
                "Click the button on a row to generate tests, then review the preview below."
            )
        else:
            self.summary.setText(
                "No source files were detected in the folders you marked."
            )
            self.preview.clear()
            self.functions.clear()

    def set_proposals(
        self,
        proposals: list[ProposedFileChange],
        adapter: LanguageAdapter | None,
    ) -> None:
        self._proposals = list(proposals)
        self._adapter = adapter
        self._refresh_action_cells()
        self._refresh_preview()

    def set_generation_enabled(self, enabled: bool, *, reason: str = "") -> None:
        self._ai_ready = enabled
        self._ai_reason = reason
        self._refresh_action_cells()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_action_cells()

    def set_row_progress(self, source_file: Path, message: str) -> None:
        for row, gap in enumerate(self._gaps):
            if gap.source_file.resolve() == source_file.resolve():
                widget = self.table.cellWidget(row, _COL_ACTION)
                if isinstance(widget, QLabel) and isValid(widget):
                    widget.setToolTip(message)
                elif isinstance(widget, QPushButton) and isValid(widget):
                    widget.setToolTip(message)
                break
        self._refresh_preview_if_selected(source_file)

    def begin_stream(self, source_file: Path) -> None:
        """Clear the live model output and select the row that is about to run."""
        key = source_file.resolve()
        self._stream_text[key] = ""
        self._stream_channel.pop(key, None)
        self._select_row_for(source_file)
        self.preview.set_live_text("")

    def append_stream_text(self, source_file: Path, channel: str, text: str) -> None:
        """Append a chunk of live model output for ``source_file``."""
        if not text:
            return
        key = source_file.resolve()
        chunk = text
        if channel != self._stream_channel.get(key):
            self._stream_channel[key] = channel
            heading = _STREAM_HEADINGS.get(channel, "")
            if heading:
                separator = "\n\n" if self._stream_text.get(key) else ""
                chunk = f"{separator}{heading}\n{text}"

        buffer = self._stream_text.get(key, "") + chunk
        truncated = len(buffer) > _MAX_STREAM_CHARS
        if truncated:
            buffer = buffer[-_MAX_STREAM_CHARS:]
        self._stream_text[key] = buffer

        gap = self._selected_gap()
        if gap is None or gap.source_file.resolve() != key:
            return
        if truncated:
            self.preview.set_live_text(buffer)
        else:
            self.preview.append_live_text(chunk)

    def _select_row_for(self, source_file: Path) -> None:
        key = source_file.resolve()
        for row, gap in enumerate(self._gaps):
            if gap.source_file.resolve() == key:
                self.table.selectRow(row)
                return

    def set_row_status(
        self,
        source_file: Path,
        status: str,
        *,
        test_count: int = 0,
        tooltip: str = "",
    ) -> None:
        self._generation_status[source_file.resolve()] = status
        self._refresh_action_cells()
        self._refresh_preview_if_selected(source_file)

    def _on_selection_changed(self) -> None:
        if self._suppress_selection_events:
            return
        self._refresh_functions()
        self._refresh_preview()

    def _refresh_functions(self) -> None:
        gap = self._selected_gap()
        if gap is None:
            self.functions.clear()
            return
        self.functions.show_gap(gap, set(self._selection_for(gap)))

    def _on_functions_changed(self) -> None:
        gap = self._selected_gap()
        if gap is None:
            return
        self._selected_functions[gap.source_file.resolve()] = (
            self.functions.checked_functions()
        )
        self._refresh_action_cells()

    def _selection_for(self, gap: QualityGap) -> list[str]:
        """Symbols ticked for ``gap``; defaults to the ones lacking coverage."""
        key = gap.source_file.resolve()
        if key not in self._selected_functions:
            self._selected_functions[key] = gap.default_selection()
        return self._selected_functions[key]

    def _selection_for_source(self, source_file: Path) -> list[str]:
        for gap in self._gaps:
            if gap.source_file.resolve() == source_file.resolve():
                return list(self._selection_for(gap))
        return []

    def _selected_gap(self) -> QualityGap | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if row < 0 or row >= len(self._gaps):
            return None
        return self._gaps[row]

    def _find_proposal(self, source_file: Path) -> ProposedFileChange | None:
        resolved = source_file.resolve()
        for change in self._proposals:
            if change.source_file is not None and change.source_file.resolve() == resolved:
                return change
        return None

    def _refresh_preview_if_selected(self, source_file: Path) -> None:
        gap = self._selected_gap()
        if gap is not None and gap.source_file.resolve() == source_file.resolve():
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        gap = self._selected_gap()
        if gap is None or self._root is None:
            self.preview.clear()
            return

        status = self._generation_status.get(gap.source_file.resolve(), "idle")
        change = self._find_proposal(gap.source_file)
        if change is not None and change.tests and self._adapter is not None:
            self.preview.show_proposal(gap, change, self._adapter, self._root)
            return

        self.preview.show_gap_details(gap, status=status)
        if status == "generating":
            self.preview.set_live_text(
                self._stream_text.get(gap.source_file.resolve(), "")
            )

    def _keep_user_choices(self) -> None:
        """Carry the ticked functions and chosen UT paths over to the new gap list.

        Choices are keyed by source file, so a rebuild (a retry after a failed or
        cancelled run, or a new analysis) no longer falls back to "everything".
        """
        kept_functions: dict[Path, list[str]] = {}
        kept_targets: dict[Path, Path] = {}
        for gap in self._gaps:
            key = gap.source_file.resolve()
            chosen = self._selected_functions.get(key)
            if chosen is not None:
                # An empty list is a real choice ("nothing ticked") and is kept as is;
                # only names the file no longer declares are dropped.
                selectable = set(gap.selectable_functions())
                kept_functions[key] = [name for name in chosen if name in selectable]
            target = self._target_overrides.get(key)
            if target is not None:
                kept_targets[key] = target
        self._selected_functions = kept_functions
        self._target_overrides = kept_targets

    def _rebuild_table(self, *, keep_source: Path | None = None) -> None:
        self.table.setRowCount(0)
        if self._root is None:
            return

        self.table.setRowCount(len(self._gaps))
        for row, gap in enumerate(self._gaps):
            try:
                rel_source = gap.source_file.relative_to(self._root)
            except ValueError:
                rel_source = gap.source_file
            source_item = _table_item(str(rel_source), tooltip=str(rel_source))
            self.table.setItem(row, _COL_SOURCE, source_item)

            name_item = _table_item(
                gap.source_file.name, tooltip=str(gap.source_file)
            )
            self.table.setItem(row, _COL_FILE_NAME, name_item)

            self._set_existing_ut_cell(row, gap)

            status = self._generation_status.get(gap.source_file.resolve(), "idle")
            self._set_action_widget(row, gap, status)

        self._fit_table_to_contents()

        if self._gaps:
            row_to_select = 0
            if keep_source is not None:
                for row, gap in enumerate(self._gaps):
                    if gap.source_file.resolve() == keep_source:
                        row_to_select = row
                        break
            self._suppress_selection_events = True
            self.table.selectRow(row_to_select)
            self._suppress_selection_events = False
            self._refresh_functions()
            self._refresh_preview()

    def _ut_path_for(self, gap: QualityGap) -> Path | None:
        """Where the tests for ``gap`` will go: the user's choice, else the planned path."""
        return self._target_overrides.get(
            gap.source_file.resolve(), gap.existing_test_file
        )

    def _target_override_for(self, source_file: Path) -> Path | None:
        return self._target_overrides.get(source_file.resolve())

    def _can_choose_ut_path(self, gap: QualityGap) -> bool:
        """The destination may be changed only while the test file does not exist yet."""
        path = self._ut_path_for(gap)
        return path is None or not path.is_file()

    def _ut_cell_text(self, path: Path | None) -> str:
        if path is None:
            return "—"
        if self._root is not None:
            try:
                return str(path.relative_to(self._root))
            except ValueError:
                pass
        return str(path)

    def _set_existing_ut_cell(self, row: int, gap: QualityGap) -> None:
        path = self._ut_path_for(gap)
        text = self._ut_cell_text(path)

        if not self._can_choose_ut_path(gap):
            self.table.removeCellWidget(row, _COL_EXISTING_UT)
            self.table.setItem(
                row, _COL_EXISTING_UT, _table_item(text, tooltip=str(path))
            )
            return

        # Placeholder item so row selection/sizing keeps working behind the widget.
        self.table.setItem(row, _COL_EXISTING_UT, _table_item(""))

        container = QWidget()
        box = QHBoxLayout(container)
        box.setContentsMargins(4, 0, 4, 0)
        box.setSpacing(6)

        label = QLabel(text)
        tooltip = (
            f"{path}\nThis file does not exist yet — it will be created."
            if path is not None
            else "No destination yet — click ... to choose one."
        )
        label.setToolTip(tooltip)
        box.addWidget(label, 1)

        button = QPushButton(_CHOOSE_PATH_LABEL)
        button.setFixedWidth(28)
        button.setToolTip("Choose where the new unit-test file should be created")
        button.clicked.connect(lambda _checked=False, r=row: self._choose_ut_path(r))
        box.addWidget(button, 0)

        self.table.setCellWidget(row, _COL_EXISTING_UT, container)

    def _choose_ut_path(self, row: int) -> None:
        if row < 0 or row >= len(self._gaps):
            return
        gap = self._gaps[row]
        if not self._can_choose_ut_path(gap):
            return

        suggested = self._ut_path_for(gap)
        if suggested is None:
            base_dir = self._root or gap.source_file.parent
            suggested = base_dir / f"{gap.source_file.stem}UT{gap.source_file.suffix}"

        start_dir = suggested.parent
        while not start_dir.is_dir() and start_dir != start_dir.parent:
            start_dir = start_dir.parent

        suffix = suggested.suffix or gap.source_file.suffix
        chosen, _selected_filter = QFileDialog.getSaveFileName(
            self,
            f"Unit-test file for {gap.source_file.name}",
            str(start_dir / suggested.name),
            f"Test files (*{suffix});;All files (*)" if suffix else "All files (*)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not chosen:
            return

        new_path = Path(chosen)
        if not new_path.suffix and suffix:
            new_path = new_path.with_suffix(suffix)

        self._target_overrides[gap.source_file.resolve()] = new_path
        self._set_existing_ut_cell(row, gap)
        self._fit_table_to_contents()

    def _refresh_action_cells(self) -> None:
        for row, gap in enumerate(self._gaps):
            status = self._generation_status.get(gap.source_file.resolve(), "idle")
            test_count = 0
            change = self._find_proposal(gap.source_file)
            if change is not None:
                test_count = len(change.tests)
            self._set_action_widget(row, gap, status, test_count=test_count)
        self._fit_table_to_contents()

    def _fit_table_to_contents(self) -> None:
        if self.table.rowCount() == 0:
            return
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()
        header = self.table.horizontalHeader()
        min_widths = {
            _COL_SOURCE: 140,
            _COL_FILE_NAME: 120,
            _COL_EXISTING_UT: 120,
            _COL_ACTION: 140,
        }
        for column, minimum in min_widths.items():
            if header.sectionSize(column) < minimum:
                header.resizeSection(column, minimum)

    def _set_action_widget(
        self,
        row: int,
        gap: QualityGap,
        status: str,
        *,
        test_count: int = 0,
        tooltip: str = "",
    ) -> None:
        if status == "generating":
            label = QLabel(_GENERATING_LABEL)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, _COL_ACTION, label)
            return

        if status in {"done", "applied"}:
            container = QWidget()
            box = QHBoxLayout(container)
            box.setContentsMargins(4, 0, 4, 0)
            box.setSpacing(6)
            box.addStretch(1)

            label = QLabel(_GENERATED_LABEL)
            label.setStyleSheet("color: #22863a;")
            box.addWidget(label, 0)

            regenerate = QPushButton(_REGENERATE_LABEL)
            regenerate.setFixedWidth(28)
            regenerate.clicked.connect(
                lambda _checked=False, p=gap.source_file: self.regenerate_requested.emit(
                    p, self._selection_for_source(p), self._target_override_for(p)
                )
            )
            box.addWidget(regenerate, 0)
            box.addStretch(1)

            self.table.setCellWidget(row, _COL_ACTION, container)
            self._generate_button_state(
                regenerate, gap, status, tooltip=_REGENERATE_TOOLTIP
            )
            return

        button = QPushButton(
            _NEW_TESTS_LABEL if gap.kind == GAP_NO_TEST_FILE else _MORE_TESTS_LABEL
        )
        button.clicked.connect(
            lambda _checked=False, p=gap.source_file: self.preview_requested.emit(
                p, self._selection_for_source(p), self._target_override_for(p)
            )
        )
        self.table.setCellWidget(row, _COL_ACTION, button)
        self._generate_button_state(button, gap, status, tooltip=tooltip)

    def _generate_button_state(
        self,
        button: QPushButton,
        gap: QualityGap,
        status: str,
        *,
        tooltip: str = "",
    ) -> None:
        if not isValid(button):
            return
        generation_active = any(
            s == "generating" for s in self._generation_status.values()
        )
        generating = status == "generating"
        has_symbols = bool(gap.selectable_functions())
        selection = self._selection_for(gap)
        nothing_selected = has_symbols and not selection
        enabled = (
            self._ai_ready
            and not generating
            and not generation_active
            and not nothing_selected
        )
        button.setEnabled(enabled)
        if generating:
            button.setToolTip("Generating tests for this file…")
        elif generation_active:
            button.setToolTip("Wait for the current generation to finish.")
        elif not self._ai_ready:
            button.setToolTip(self._ai_reason or "AI generation unavailable.")
        elif nothing_selected:
            button.setToolTip(
                "Tick at least one function in the list below to generate tests for it."
            )
        elif tooltip:
            button.setToolTip(tooltip)
        elif status in {"failed", "no_tests"}:
            button.setToolTip("Click to try generating tests again.")
        elif has_symbols:
            button.setToolTip(
                f"Generate tests for the {len(selection)} selected function(s)."
            )
        elif gap.has_existing_tests:
            button.setToolTip(
                "Generate additional test cases to append to the existing test file."
            )
        else:
            button.setToolTip("Generate unit tests for this source file.")
