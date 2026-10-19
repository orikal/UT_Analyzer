"""Main application window: wires the widgets to the pipeline orchestrator."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..core import orchestrator
from ..core.detection.test_content_sampler import planned_test_directory
from ..core.generation.ai_runtime_manager import AIRuntimeManager, RuntimeState, RuntimeStatus
from ..core.generation.generation_trace import trace, trace_path
from ..core.reporting.report_builder import render_quality_report_html
from ..models import ProjectConfig
from .threading import Worker
from .widgets.approved_tests_tab import ApprovedTestsTab
from .widgets.collapsible_sidebar import CollapsibleSidebar
from .widgets.diagnostics_dialog import DiagnosticsDialog
from .widgets.folder_tree import FolderTree
from .widgets.notifying_tab_widget import NotifyingTabWidget
from .widgets.project_selector import ProjectSelector
from .widgets.proposal_tabs import FileChangeTab
from .widgets.report_view import ReportView
from .widgets.source_files_tab import SourceFilesTab
from .widgets.settings_dialog import (
    SettingsDialog,
    load_max_tests_per_file,
    load_llm_settings,
)


class MainWindow(QMainWindow):
    def __init__(self, runtime_status: RuntimeStatus | None = None) -> None:
        super().__init__()
        self.setWindowTitle("UT Analyzer")
        self.resize(1400, 900)

        self.session: orchestrator.AnalysisSession | None = None
        self._worker: Worker | None = None
        self._change_tabs: list[FileChangeTab] = []
        self._source_files_tab: SourceFilesTab | None = None
        self._approved_tests_tab: ApprovedTestsTab | None = None
        self._generating_source: Path | None = None
        self._runtime_status = runtime_status or AIRuntimeManager.instance().status

        self._build_toolbar()
        self._build_menu()
        self._build_layout()
        self._build_statusbar()
        self._update_action_states()
        self._update_ai_status()

    # ---- UI construction ---------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.retry_ai_action = QAction("Retry AI Runtime", self)
        self.retry_ai_action.triggered.connect(self._retry_ai_runtime)
        toolbar.addAction(self.retry_ai_action)

    def _build_menu(self) -> None:
        settings_menu = self.menuBar().addMenu("Settings")
        settings_menu.addAction("Preferences...", self._open_settings)

        self.menuBar().addAction("Diagnostics", self._open_diagnostics)

    def _build_layout(self) -> None:
        sidebar_content = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_content)
        sidebar_layout.setContentsMargins(6, 6, 6, 6)

        self.selector = ProjectSelector()
        self.selector.project_changed.connect(self._on_project_changed)
        self.selector.scan_requested.connect(self._start_scan)
        sidebar_layout.addWidget(self.selector)

        self.folder_tree = FolderTree()
        sidebar_layout.addWidget(self.folder_tree, 1)

        self._central_host = QWidget()
        host_layout = QVBoxLayout(self._central_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        self._sidebar = CollapsibleSidebar(
            sidebar_content,
            host=self._central_host,
        )
        self._sidebar.expanded_changed.connect(self._on_sidebar_toggled)
        self._splitter.addWidget(self._sidebar)

        self.tabs = NotifyingTabWidget()
        self.tabs.setTabsClosable(False)
        self.report_view = ReportView(
            "Select a project directory and press 'Scan && Analyze' to begin."
        )
        self.tabs.addTab(self.report_view, "Analysis Report")
        self._splitter.addWidget(self.tabs)
        self._splitter.setCollapsible(0, True)
        self._splitter.setSizes([420, 980])

        host_layout.addWidget(self._splitter)
        self.setCentralWidget(self._central_host)

    def _on_sidebar_toggled(self, expanded: bool) -> None:
        sizes = self._splitter.sizes()
        if len(sizes) < 2:
            return
        total = sum(sizes)
        if expanded:
            width = self._sidebar.saved_width()
            self._splitter.setSizes([width, max(total - width, 200)])
        else:
            self._sidebar.set_saved_width(max(sizes[0], 280))
            self._splitter.setSizes([0, total])

    def _build_statusbar(self) -> None:
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumWidth(180)
        self.progress_bar.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_worker)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.statusBar().showMessage("Ready")

    # ---- state helpers --------------------------------------------------------

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _ai_ready(self) -> bool:
        return self._runtime_status.state == RuntimeState.READY

    def _scan_succeeded(self) -> bool:
        return (
            self.session is not None
            and self.session.adapter is not None
            and self.session.quality is not None
        )

    def _update_action_states(self) -> None:
        busy = self._busy()
        self.selector.set_scan_enabled(not busy)
        if self._source_files_tab is not None:
            ai_reason = ""
            if not self._ai_ready():
                ai_reason = (
                    self._runtime_status.technical_reason
                    or self._runtime_status.message
                    or "AI generation unavailable"
                )
            self._source_files_tab.set_generation_enabled(
                not busy and self._scan_succeeded() and self._ai_ready(),
                reason=ai_reason,
            )
            self._source_files_tab.set_busy(busy)
        if self._approved_tests_tab is not None:
            self._approved_tests_tab.set_busy(busy)
        self.retry_ai_action.setEnabled(not busy and not self._ai_ready())

    def _run_worker(self, fn, on_done) -> None:
        worker = Worker(fn, parent=self)
        worker.progress.connect(self._on_worker_progress)
        worker.stream.connect(self._on_worker_stream)
        worker.finished_ok.connect(lambda result: self._worker_finished(on_done, result))
        worker.failed.connect(self._worker_failed)
        worker.cancelled.connect(self._worker_cancelled)
        self._worker = worker
        self.progress_bar.setVisible(True)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self._update_action_states()
        worker.start()

    def _on_worker_progress(self, message: str) -> None:
        self.statusBar().showMessage(message)
        if self._generating_source is not None and self._source_files_tab is not None:
            self._source_files_tab.set_row_progress(self._generating_source, message)

    def _on_worker_stream(self, channel: str, text: str) -> None:
        """Show the model's live output in the Suggested Tests preview."""
        if self._generating_source is None or self._source_files_tab is None:
            return
        self._source_files_tab.append_stream_text(
            self._generating_source, channel, text
        )

    def _worker_finished(self, on_done, result) -> None:
        try:
            on_done(result)
        finally:
            self._worker = None
            self.progress_bar.setVisible(False)
            self.cancel_button.setVisible(False)
            self.statusBar().showMessage("Done", 5000)
            self._update_action_states()

    def _worker_failed(self, message: str) -> None:
        message = message or "Unknown error"
        short = message.split("\n", 1)[0] or "Unknown error"
        try:
            self._reset_generation_row_on_abort(tooltip=short)
            head, _, tail = message.partition("\n\nTraceback (most recent call last):")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Operation failed")
            box.setText(short)
            body = head[len(short):].strip()
            if body:
                box.setInformativeText(body)
            if tail:
                box.setDetailedText("Traceback (most recent call last):" + tail)
            box.exec()
        finally:
            self._worker = None
            self.progress_bar.setVisible(False)
            self.cancel_button.setVisible(False)
            self.statusBar().showMessage(f"Failed — {short}", 15000)
            self._update_action_states()

    def _worker_cancelled(self) -> None:
        try:
            self._reset_generation_row_on_abort(idle=True)
        finally:
            self._worker = None
            self.progress_bar.setVisible(False)
            self.cancel_button.setVisible(False)
            self.statusBar().showMessage("Operation cancelled", 5000)
            self._update_action_states()

    def _cancel_worker(self) -> None:
        if self._worker is not None:
            self.cancel_button.setEnabled(False)
            self._worker.cancel()
            self.statusBar().showMessage("Cancelling...")

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Operation in progress",
                "An operation is still running. Cancel and exit?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.cancel()
            self._worker.wait(3000)
        AIRuntimeManager.instance().shutdown()
        super().closeEvent(event)

    # ---- AI runtime -----------------------------------------------------------

    def _update_ai_status(self) -> None:
        status = self._runtime_status
        ready = status.state == RuntimeState.READY
        reason = status.technical_reason if not ready else ""
        model = status.model
        self.selector.set_ai_status(ready, model, reason=reason)
        if ready:
            self.statusBar().showMessage("AI runtime ready", 5000)
        elif status.state in (RuntimeState.FAILED, RuntimeState.DEGRADED):
            self.statusBar().showMessage(
                f"AI generation unavailable — {status.technical_reason or status.message}",
                15000,
            )
        self._update_action_states()

    def _retry_ai_runtime(self) -> None:
        self.statusBar().showMessage("Restarting AI runtime...")
        settings = load_llm_settings()
        runtime = AIRuntimeManager.instance()
        self._runtime_status = runtime.retry(settings)
        self._update_ai_status()
        if self._ai_ready():
            QMessageBox.information(self, "AI runtime", "AI runtime is ready.")
        else:
            QMessageBox.warning(
                self,
                "AI runtime",
                f"AI generation unavailable.\n\n{self._runtime_status.technical_reason}",
            )

    # ---- events -------------------------------------------------------------------

    def _on_project_changed(self, path_text: str) -> None:
        path = Path(path_text)
        if not path.is_dir():
            return
        self.folder_tree.set_root(path)
        self.selector.set_test_dir(str(planned_test_directory(path)))

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec():
            settings = load_llm_settings()
            runtime = AIRuntimeManager.instance()
            self._runtime_status = runtime.retry(settings)
            self._update_ai_status()

    def _open_diagnostics(self) -> None:
        DiagnosticsDialog(self).exec()

    # ---- pipeline steps ---------------------------------------------------------------

    def _start_scan(self) -> None:
        root = self.selector.project_path()
        if root is None:
            QMessageBox.warning(
                self, "No project", "Please select a valid project directory first."
            )
            return
        marked = self.folder_tree.checked_dirs()
        if not marked:
            QMessageBox.warning(
                self, "Nothing selected", "Check at least one folder in the tree."
            )
            return
        # The scan always covers the whole project so the analyzer sees every
        # dependency; the marked folders only limit what Suggested Tests shows.
        config = ProjectConfig(
            root=root,
            test_dir=self.selector.test_dir_path(),
            included_dirs=[root],
            focus_dirs=marked,
        )

        self.session = None
        self._clear_dynamic_tabs()
        self.report_view.set_placeholder("Scanning...")

        def job(progress, cancelled):
            return orchestrator.run_scan_and_analyze(config, progress, cancelled)

        self._run_worker(job, self._on_scan_done)

    def _on_scan_done(self, session: orchestrator.AnalysisSession) -> None:
        self.session = session
        if session.env is not None:
            self.selector.set_environment(session.env)
        if session.adapter is None or session.quality is None:
            self.report_view.set_report_html(
                "<p style='color:#cb2431'>Could not detect the project language in "
                "this project. Make sure the project folder contains source code.</p>"
            )
            return
        self.report_view.set_report_html(
            render_quality_report_html(
                session.env,
                session.quality,
                session.repo_knowledge,
                session.knowledge_docs,
            )
        )
        self._show_source_files_tab(session)
        gap_count = len(session.quality.gaps)
        missing_count = sum(1 for g in session.quality.gaps if not g.has_existing_tests)
        existing_count = gap_count - missing_count
        self.statusBar().showMessage(
            f"Analysis complete — {missing_count} file(s) without unit tests, "
            f"{existing_count} file(s) with tests that can get more cases. "
            + ("Open the Suggested Tests tab and pick a row." if gap_count else ""),
            10000,
        )

    def _show_source_files_tab(self, session: orchestrator.AnalysisSession) -> None:
        if self._source_files_tab is None:
            self._source_files_tab = SourceFilesTab()
            self._source_files_tab.preview_requested.connect(self._start_preview_generation)
            self._source_files_tab.regenerate_requested.connect(self._start_regeneration)
            self._source_files_tab.apply_requested.connect(self._start_apply_for)
        self._source_files_tab.set_gaps(
            session.quality,
            session.config.root,
            generation_status=session.generation_status,
        )
        self._source_files_tab.set_proposals(session.proposals, session.adapter)
        if self.tabs.indexOf(self._source_files_tab) < 0:
            self.tabs.insertTab(1, self._source_files_tab, "Suggested Tests")
            self.tabs.mark_tab_notified(self._source_files_tab)
        self._update_action_states()

    def _start_regeneration(
        self, source_file: Path, functions: object = None, target_test_file: object = None
    ) -> None:
        """Refresh button next to 'Generated.' — ask the model for another round of tests."""
        self._start_preview_generation(
            source_file, functions, target_test_file, regenerate=True
        )

    def _start_preview_generation(
        self,
        source_file: Path,
        functions: object = None,
        target_test_file: object = None,
        *,
        regenerate: bool = False,
    ) -> None:
        if self.session is None:
            QMessageBox.information(
                self,
                "Nothing analyzed yet",
                "Run Scan & Analyze first, then pick a file to generate tests for.",
            )
            return
        if self._busy():
            self.statusBar().showMessage(
                "Generation already in progress — wait for it to finish or click Cancel.",
                8000,
            )
            return
        if not self._ai_ready():
            reason = (
                self._runtime_status.technical_reason
                or self._runtime_status.message
                or "The AI runtime is not ready."
            )
            QMessageBox.warning(
                self,
                "AI generation unavailable",
                "Tests cannot be generated because the AI runtime is not ready.\n\n"
                f"{reason}\n\n"
                "Check the model settings, then use Retry AI runtime from the toolbar.",
            )
            return
        gap = orchestrator.find_gap_for_source(self.session, source_file)
        if gap is None:
            QMessageBox.information(
                self,
                "No testing gap for this file",
                f"'{source_file.name}' is no longer listed as a testing gap in the "
                "current analysis.\n\nRun Scan & Analyze again to refresh the list.",
            )
            return

        resolved = source_file.resolve()
        status = self.session.generation_status.get(resolved, "idle")
        existing = self._find_proposal_for_source(source_file)
        if (
            not regenerate
            and existing is not None
            and existing.tests
            and status not in {"failed", "no_tests"}
        ):
            self.statusBar().showMessage(
                f"{len(existing.tests)} test(s) were already generated for "
                f"{source_file.name} — review them below or in the "
                f"'{existing.path.name}' tab.",
                10000,
            )
            return

        settings = load_llm_settings()
        max_tests = load_max_tests_per_file()
        session = self.session
        selected = [str(name) for name in (functions or [])]
        target_path = Path(target_test_file) if target_test_file else None
        self._generating_source = resolved
        trace(f"UI preview generation for {source_file.name}")
        if self._source_files_tab is not None:
            self._source_files_tab.begin_stream(source_file)
            self._source_files_tab.set_row_status(source_file, "generating")
            self._source_files_tab.set_busy(True)

        def job(progress, cancelled):
            return orchestrator.run_generation_for_gap(
                session,
                gap,
                settings,
                max_tests,
                progress,
                cancelled,
                selected_functions=selected,
                target_test_file=target_path,
            )

        self._run_worker(job, self._on_preview_generation_done)

    def _on_preview_generation_done(self, change) -> None:
        source = self._generating_source
        self._generating_source = None

        if source is not None and self._source_files_tab is not None:
            if change is None:
                self._source_files_tab.set_row_status(source, "no_tests")
                self.statusBar().showMessage(
                    "The model returned no usable tests — see the message for details.",
                    12000,
                )
            else:
                self._source_files_tab.set_row_status(
                    source, "done", test_count=len(change.tests)
                )
            assert self.session is not None
            self._source_files_tab.set_proposals(
                self.session.proposals, self.session.adapter
            )
            self._source_files_tab.set_busy(False)

        if change is None:
            QMessageBox.warning(
                self,
                "No tests were generated",
                "The model finished but produced no usable test cases"
                + (f" for '{source.name}'." if source is not None else ".")
                + "\n\nWhat to try:\n"
                "- Click the row button again to retry.\n"
                "- Reduce 'Max tests per file' in Settings.\n"
                "- Switch to a code-oriented model in Settings.\n"
                f"- Inspect the raw model reply in the trace log:\n  {trace_path()}",
            )
            self._update_action_states()
            return

        assert self.session is not None
        self._ensure_change_tab(change)
        self.statusBar().showMessage(
            f"Preview ready — {len(change.tests)} test(s) for "
            f"{change.source_file.name if change.source_file else change.path.name}. "
            "Review below, then click Apply to Repo.",
            10000,
        )
        self._update_action_states()

    def _start_apply_for(self, source_file: Path) -> None:
        if self.session is None or self._busy():
            return
        change = self._find_proposal_for_source(source_file)
        if change is None or not change.tests:
            QMessageBox.warning(
                self,
                "No tests to apply",
                "Select the source file row first to load a test preview.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Apply tests to repository",
            f"Write {len(change.tests)} proposed test(s) for "
            f"{source_file.name} to the project?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        session = self.session
        self._generating_source = source_file.resolve()
        if self._source_files_tab is not None:
            self._source_files_tab.set_busy(True)

        def job(progress, cancelled):
            return orchestrator.run_apply_for_source(
                session, source_file, progress, cancelled
            )

        self._run_worker(job, self._on_apply_for_done)

    def _on_apply_for_done(self, outcome) -> None:
        source = self._generating_source
        self._generating_source = None
        if self._source_files_tab is not None:
            self._source_files_tab.set_busy(False)

        if source is not None and self._source_files_tab is not None:
            if outcome.written:
                self._source_files_tab.set_row_status(source, "applied")
            elif outcome.errors:
                self._source_files_tab.set_row_status(
                    source,
                    "failed",
                    tooltip=outcome.errors[0][1],
                )

        if outcome.errors:
            QMessageBox.warning(
                self,
                "Apply failed",
                "\n".join(f"{path}: {reason}" for path, reason in outcome.errors),
            )
        elif outcome.written:
            self.statusBar().showMessage(
                f"Wrote test file: {outcome.written[0].name}",
                10000,
            )
        self._update_action_states()

    def _ensure_change_tab(self, change) -> None:
        """Keep a review tab available without leaving the Suggested Tests view."""
        tab = self._find_change_tab(change.path)
        if tab is not None:
            tab.append_tests(change.tests)
            self._sync_approved_tab()
            return
        assert self.session is not None
        tab = FileChangeTab(change, self.session.adapter, self.session.config.root)
        tab.state_changed.connect(self._on_change_tab_state_changed)
        label = change.path.name + (" *" if change.is_new_file else "")
        insert_at = self._insert_index_before_approved()
        self.tabs.insertTab(insert_at, tab, label)
        self.tabs.mark_tab_notified(tab)
        self._change_tabs.append(tab)
        self._sync_approved_tab()

    def _on_change_tab_state_changed(self) -> None:
        self._sync_approved_tab()
        self._update_action_states()

    def _sync_approved_tab(self) -> None:
        if self.session is None:
            return
        if self._approved_tests_tab is None:
            self._approved_tests_tab = ApprovedTestsTab()
            self._approved_tests_tab.apply_requested.connect(self._start_apply_single_test)
            self._approved_tests_tab.revert_requested.connect(self._start_revert_single_test)

        has_approved = self._approved_tests_tab.refresh(
            self.session.proposals,
            self.session.adapter,
            self.session.config.root,
        )
        tab_index = self.tabs.indexOf(self._approved_tests_tab)
        if has_approved:
            if tab_index < 0:
                self.tabs.addTab(self._approved_tests_tab, "Approved Tests")
                self.tabs.mark_tab_notified(self._approved_tests_tab)
        elif tab_index >= 0:
            self.tabs.removeTab(tab_index)

    def _insert_index_before_approved(self) -> int:
        if self._approved_tests_tab is not None:
            idx = self.tabs.indexOf(self._approved_tests_tab)
            if idx >= 0:
                return idx
        return self.tabs.count()

    def _confirm_apply_single_test(self, change_path: Path, test_id: int) -> bool:
        """Ask the user to confirm which files are created/updated before writing."""
        if self.session is None:
            return False
        resolved = change_path.resolve()
        change = next(
            (c for c in self.session.proposals if c.path.resolve() == resolved), None
        )
        if change is None:
            return True
        test = next((t for t in change.tests if t.id == test_id), None)

        root = self.session.config.root

        def label(path: Path) -> str:
            try:
                return str(path.relative_to(root))
            except ValueError:
                return str(path)

        created: list[str] = []
        updated: list[str] = []
        (created if not change.path.is_file() else updated).append(label(change.path))
        if change.header_path is not None and change.header_content:
            if not change.header_path.is_file():
                created.append(label(change.header_path))

        lines: list[str] = []
        if test is not None:
            lines.append(f"Test: {test.name}")
            lines.append("")
        if created:
            lines.append("Files that will be created:")
            lines.extend(f"  + {name}" for name in created)
        if updated:
            if created:
                lines.append("")
            lines.append("Files that will be updated:")
            lines.extend(f"  ~ {name}" for name in updated)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Apply to code")
        box.setText("Write this test to the project?")
        box.setInformativeText("\n".join(lines))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        box.button(QMessageBox.StandardButton.Yes).setText("Apply")
        return box.exec() == QMessageBox.StandardButton.Yes

    def _start_apply_single_test(self, change_path: Path, test_id: int) -> None:
        if self.session is None or self._busy():
            return
        session = self.session

        if not self._confirm_apply_single_test(change_path, test_id):
            return

        def job(progress, cancelled):
            return orchestrator.run_apply_single_test(
                session, change_path, test_id, progress, cancelled
            )

        if self._approved_tests_tab is not None:
            self._approved_tests_tab.set_busy(True)
        self._run_worker(job, self._on_single_apply_done)

    def _start_revert_single_test(self, change_path: Path, test_id: int) -> None:
        if self.session is None or self._busy():
            return
        session = self.session

        answer = QMessageBox.question(
            self,
            "Delete test from code",
            f"Remove this test from {change_path.name}?\n\n"
            "If it is the last test in the file, the files created when it was "
            "applied (test file and companion header) are deleted too.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def job(progress, cancelled):
            return orchestrator.run_revert_single_test(
                session, change_path, test_id, progress, cancelled
            )

        if self._approved_tests_tab is not None:
            self._approved_tests_tab.set_busy(True)
        self._run_worker(job, self._on_single_revert_done)

    def _on_single_revert_done(self, outcome) -> None:
        if self._approved_tests_tab is not None:
            self._approved_tests_tab.set_busy(False)
        self._sync_approved_tab()
        for tab in self._change_tabs:
            tab.refresh_cards()

        if outcome.errors:
            QMessageBox.warning(
                self,
                "Delete failed",
                "\n".join(f"{path}: {reason}" for path, reason in outcome.errors),
            )
        elif outcome.deleted:
            QMessageBox.information(
                self,
                "Deleted from code",
                "Last test removed. Deleted file(s):\n"
                + "\n".join(str(path) for path in outcome.deleted),
            )
        elif outcome.updated:
            self.statusBar().showMessage(
                f"Removed test from: {outcome.updated[0].name}",
                10000,
            )
        elif outcome.skipped:
            QMessageBox.warning(
                self,
                "Nothing deleted",
                "\n".join(f"{path}: {reason}" for path, reason in outcome.skipped),
            )
        self._update_action_states()

    def _on_single_apply_done(self, outcome) -> None:
        if self._approved_tests_tab is not None:
            self._approved_tests_tab.set_busy(False)
        self._sync_approved_tab()
        for tab in self._change_tabs:
            tab.refresh_cards()

        if outcome.errors:
            QMessageBox.warning(
                self,
                "Apply failed",
                "\n".join(f"{path}: {reason}" for path, reason in outcome.errors),
            )
        elif outcome.written:
            self.statusBar().showMessage(
                f"Wrote test to: {outcome.written[0].name}",
                10000,
            )
        self._update_action_states()

    # ---- helpers ----------------------------------------------------------------------

    def _reset_generation_row_on_abort(self, *, idle: bool = False, tooltip: str = "") -> None:
        source = self._generating_source
        self._generating_source = None
        if source is None or self._source_files_tab is None or self.session is None:
            if self._source_files_tab is not None:
                self._source_files_tab.set_busy(False)
            return
        if idle:
            # Only the row goes back to "idle" — rebuilding the whole table here
            # would throw away the functions the user ticked for this file.
            self.session.generation_status.pop(source, None)
            self._source_files_tab.set_row_status(source, "idle")
        else:
            self.session.generation_status[source] = "failed"
            self._source_files_tab.set_row_status(source, "failed", tooltip=tooltip)
        self._source_files_tab.set_busy(False)

    def _find_proposal_for_source(self, source_file: Path):
        if self.session is None:
            return None
        resolved = source_file.resolve()
        for change in self.session.proposals:
            if change.source_file is not None and change.source_file.resolve() == resolved:
                return change
        return None

    def _find_change_tab(self, target_path: Path) -> FileChangeTab | None:
        resolved = target_path.resolve()
        for tab in self._change_tabs:
            if tab.change.path.resolve() == resolved:
                return tab
        return None

    def _remove_proposal_for_source(self, source_file: Path) -> None:
        if self.session is None:
            return
        resolved = source_file.resolve()
        self.session.proposals = [
            c
            for c in self.session.proposals
            if c.source_file is None or c.source_file.resolve() != resolved
        ]
        if self.session.approval is not None:
            self.session.approval.set_changes(self.session.proposals)
        tab = next(
            (
                t
                for t in self._change_tabs
                if t.change.source_file is not None
                and t.change.source_file.resolve() == resolved
            ),
            None,
        )
        if tab is not None:
            index = self.tabs.indexOf(tab)
            if index >= 0:
                self.tabs.removeTab(index)
            self._change_tabs.remove(tab)
            tab.deleteLater()
        if self._source_files_tab is not None:
            self._source_files_tab.set_proposals(
                self.session.proposals, self.session.adapter
            )

    def _clear_dynamic_tabs(self) -> None:
        keep = {self._source_files_tab, self._approved_tests_tab}
        while self.tabs.count() > 1:
            widget = self.tabs.widget(1)
            self.tabs.removeTab(1)
            if widget not in keep:
                widget.deleteLater()
        self._change_tabs.clear()
        if self._approved_tests_tab is not None:
            idx = self.tabs.indexOf(self._approved_tests_tab)
            if idx >= 0:
                self.tabs.removeTab(idx)
