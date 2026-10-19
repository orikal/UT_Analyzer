"""Pipeline orchestration: the session state and the long-running operations
executed on worker threads by the UI."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters.base import LanguageAdapter, load_default_adapters, registry
from ..models import (
    BeforeAfterReport,
    EnvironmentInfo,
    ProjectConfig,
    ProjectSnapshot,
    ProposedFileChange,
    ProposedTest,
    QualityGap,
    QualityReport,
    RepoKnowledge,
    ScanResult,
    TestConventions,
    TestFile,
    UTKnowledge,
)
from .conventions.convention_analyzer import analyze_conventions
from .conventions.ut_study import study_test_folder
from .cancellation import raise_if_cancelled
from .detection.environment_detector import detect_environment
from .detection.test_content_sampler import resolve_test_directory
from .diffing.approval_manager import ApprovalManager
from .diffing.patch_applier import (
    ApplyOutcome,
    RevertOutcome,
    apply_approved_changes,
    apply_single_approved_test,
    revert_single_applied_test,
)
from .discovery.test_discovery import discover_tests
from .generation.llm_client import LLMError, create_llm_client
from .generation.llm_settings import LLMSettings
from .generation.test_generator import generate_proposal_for_gap, generate_proposals
from .knowledge.knowledge_docs import KnowledgeDocs, write_knowledge_docs
from .knowledge.repo_indexer import build_repo_knowledge, write_knowledge_markdown
from .quality.quality_analyzer import analyze_quality
from .reporting.report_builder import build_before_after, make_snapshot
from .scanning.repo_scanner import scan_repository
from .validation.test_validator import validate_proposals

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]


@dataclass
class AnalysisSession:
    """All state for one analysis of one repository."""

    config: ProjectConfig
    scan: ScanResult | None = None
    env: EnvironmentInfo | None = None
    adapter: LanguageAdapter | None = None
    test_files: list[TestFile] = field(default_factory=list)
    conventions = None  # TestConventions | None
    repo_knowledge: RepoKnowledge | None = None
    ut_knowledge: UTKnowledge | None = None
    knowledge_docs: KnowledgeDocs | None = None
    quality: QualityReport | None = None
    before_snapshot: ProjectSnapshot | None = None
    proposals: list[ProposedFileChange] = field(default_factory=list)
    approval = None  # ApprovalManager | None
    before_after: BeforeAfterReport | None = None
    generation_status: dict[Path, str] = field(default_factory=dict)


def run_scan_and_analyze(
    config: ProjectConfig,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> AnalysisSession:
    """Scan -> detect environment -> discover tests -> conventions -> quality."""
    load_default_adapters()
    session = AnalysisSession(config=config)

    progress("Scanning repository...")
    session.scan = scan_repository(config, registry, progress=progress, cancelled=cancelled)
    raise_if_cancelled(cancelled)

    progress("Detecting environment...")
    session.env, session.adapter = detect_environment(config, session.scan, registry)
    raise_if_cancelled(cancelled)

    if session.adapter is not None:
        progress("Indexing the repository...")
        session.repo_knowledge = build_repo_knowledge(
            session.scan,
            session.adapter.language_id,
            progress=progress,
            cancelled=cancelled,
        )
        markdown = write_knowledge_markdown(session.repo_knowledge)
        progress(session.repo_knowledge.summary())
        if markdown is not None:
            progress(f"Repository knowledge written to {markdown}")
        raise_if_cancelled(cancelled)

        progress("Discovering existing tests...")
        session.test_files = discover_tests(
            session.scan, session.adapter, progress=progress, cancelled=cancelled
        )
        raise_if_cancelled(cancelled)

        progress("Analyzing test conventions...")
        session.conventions = analyze_conventions(
            session.test_files,
            session.adapter.language_id,
            test_dir=resolve_test_directory(config.root, config.test_dir),
        )
        raise_if_cancelled(cancelled)

        progress("Learning the existing unit tests...")
        session.ut_knowledge = study_test_folder(
            config.test_dir,
            session.test_files,
            session.adapter,
            progress=progress,
            cancelled=cancelled,
        )
        progress(session.ut_knowledge.summary())
        raise_if_cancelled(cancelled)

        progress("Analyzing test quality...")
        session.quality = analyze_quality(
            session.scan,
            session.test_files,
            session.adapter,
            session.env,
            session.config,
            conventions=session.conventions,
            cancelled=cancelled,
            progress=progress,
        )
        session.before_snapshot = make_snapshot(session.test_files, session.quality)

        progress("Writing the knowledge files...")
        session.knowledge_docs = write_knowledge_docs(
            config.root,
            env=session.env,
            scan=session.scan,
            quality=session.quality,
            conventions=session.conventions,
            ut_knowledge=session.ut_knowledge,
            repo_knowledge=session.repo_knowledge,
            test_dir=resolve_test_directory(config.root, config.test_dir),
        )
        progress(session.knowledge_docs.summary())

    session.approval = ApprovalManager()
    return session


def run_generation(
    session: AnalysisSession,
    llm_settings: LLMSettings,
    max_tests_per_file: int,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> list[ProposedFileChange]:
    """Generate proposals for the detected quality gaps."""
    if session.adapter is None or session.quality is None or session.env is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")
    if not session.quality.gaps:
        return []
    client = create_llm_client(llm_settings)
    if not client.is_available():
        raise LLMError(
            "The configured AI provider is not available. "
            "Check Settings or use Retry from the toolbar."
        )
    proposals = generate_proposals(
        gaps=session.quality.gaps,
        adapter=session.adapter,
        env=session.env,
        conventions=session.conventions or TestConventions(),
        test_files=session.test_files,
        client=client,
        test_dir=session.config.test_dir,
        root=session.config.root,
        max_tests_per_file=max_tests_per_file,
        llm_settings=llm_settings,
        progress=progress,
        cancelled=cancelled,
        scan=session.scan,
        ut_knowledge=session.ut_knowledge,
        repo_knowledge=session.repo_knowledge,
        knowledge_docs=session.knowledge_docs,
    )
    session.proposals = proposals
    assert session.approval is not None
    session.approval.set_changes(proposals)
    return proposals


def run_generation_for_gap(
    session: AnalysisSession,
    gap: QualityGap,
    llm_settings: LLMSettings,
    max_tests_per_file: int,
    progress: ProgressFn,
    cancelled: CancelFn,
    selected_functions: Sequence[str] | None = None,
    target_test_file: Path | None = None,
) -> ProposedFileChange | None:
    """Generate tests for a single source-file gap and merge into session proposals."""
    if session.adapter is None or session.quality is None or session.env is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")
    client = create_llm_client(llm_settings)
    if not client.is_available():
        raise LLMError(
            "The configured AI provider is not available. "
            "Check Settings or use Retry from the toolbar."
        )

    target_gap = gap.focused_on(selected_functions) if selected_functions else gap
    session.generation_status[gap.source_file] = "generating"
    try:
        change = generate_proposal_for_gap(
            gap=target_gap,
            adapter=session.adapter,
            env=session.env,
            conventions=session.conventions or TestConventions(),
            test_files=session.test_files,
            client=client,
            test_dir=session.config.test_dir,
            root=session.config.root,
            max_tests_per_file=max_tests_per_file,
            llm_settings=llm_settings,
            progress=progress,
            cancelled=cancelled,
            scan=session.scan,
            ut_knowledge=session.ut_knowledge,
            repo_knowledge=session.repo_knowledge,
            knowledge_docs=session.knowledge_docs,
            target_test_file=target_test_file,
        )
    except Exception:
        session.generation_status[gap.source_file] = "failed"
        raise

    if change is None:
        session.generation_status[gap.source_file] = "no_tests"
        return None

    merged = _merge_proposal(session.proposals, change)
    session.proposals = merged
    assert session.approval is not None
    session.approval.set_changes(session.proposals)
    session.generation_status[gap.source_file] = "done"
    return change


def _merge_proposal(
    proposals: list[ProposedFileChange],
    new_change: ProposedFileChange,
) -> list[ProposedFileChange]:
    """Append tests to an existing proposal for the same target file, or add a new one."""
    for existing in proposals:
        if existing.path == new_change.path:
            existing.tests.extend(new_change.tests)
            return proposals
    return proposals + [new_change]


def find_gap_for_source(session: AnalysisSession, source_file: Path) -> QualityGap | None:
    if session.quality is None:
        return None
    resolved = source_file.resolve()
    for gap in session.quality.gaps:
        if gap.source_file.resolve() == resolved:
            return gap
    return None


def run_apply_for_source(
    session: AnalysisSession,
    source_file: Path,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> ApplyOutcome:
    """Approve and write proposed tests for one source file to disk."""
    if session.adapter is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")
    resolved = source_file.resolve()
    change = next(
        (
            c
            for c in session.proposals
            if c.source_file is not None and c.source_file.resolve() == resolved
        ),
        None,
    )
    if change is None or not change.tests:
        raise RuntimeError("No proposed tests for this source file. Select the row to preview first.")
    raise_if_cancelled(cancelled)
    progress(f"Writing tests for {source_file.name}...")
    change.approve_all()
    assert session.approval is not None
    session.approval.set_changes(session.proposals)
    outcome = apply_approved_changes([change], session.adapter, session.config.root)
    if outcome.written:
        session.generation_status[source_file] = "applied"
    return outcome


def run_validation(
    session: AnalysisSession,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> None:
    if session.adapter is None or session.env is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")
    validate_proposals(
        session.proposals,
        session.adapter,
        session.env,
        session.test_files,
        session.config.root,
        progress=progress,
        cancelled=cancelled,
    )


def run_apply_and_report(
    session: AnalysisSession,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> tuple[ApplyOutcome, BeforeAfterReport]:
    """Apply approved changes, re-analyze, and build the before/after report."""
    if session.adapter is None or session.env is None or session.scan is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")

    progress("Applying approved changes...")
    outcome = apply_approved_changes(session.proposals, session.adapter, session.config.root)
    raise_if_cancelled(cancelled)

    progress("Re-scanning to measure the result...")
    new_scan = scan_repository(
        session.config, registry, progress=progress, cancelled=cancelled
    )
    raise_if_cancelled(cancelled)
    new_test_files = discover_tests(
        new_scan, session.adapter, progress=progress, cancelled=cancelled
    )
    raise_if_cancelled(cancelled)
    new_quality = analyze_quality(
        new_scan,
        new_test_files,
        session.adapter,
        session.env,
        session.config,
        conventions=session.conventions,
        cancelled=cancelled,
        progress=progress,
    )
    after_snapshot = make_snapshot(new_test_files, new_quality)

    report = build_before_after(
        before=session.before_snapshot or ProjectSnapshot(),
        after=after_snapshot,
        changes=session.proposals,
    )
    session.before_after = report
    return outcome, report


def run_apply_single_test(
    session: AnalysisSession,
    change_path: Path,
    test_id: int,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> ApplyOutcome:
    """Write one approved test from the Approved Tests tab to disk."""
    if session.adapter is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")
    change, test = _find_change_and_test(session, change_path, test_id)
    raise_if_cancelled(cancelled)
    progress(f"Writing test {test.name} to {change.path.name}...")
    return apply_single_approved_test(change, test, session.adapter, session.config.root)


def run_revert_single_test(
    session: AnalysisSession,
    change_path: Path,
    test_id: int,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> RevertOutcome:
    """Remove one previously applied test from disk (Approved Tests tab)."""
    if session.adapter is None:
        raise RuntimeError("Run 'Scan & Analyze' first.")
    change, test = _find_change_and_test(session, change_path, test_id)
    raise_if_cancelled(cancelled)
    progress(f"Removing test {test.name} from {change.path.name}...")
    outcome = revert_single_applied_test(change, test, session.adapter, session.config.root)
    if outcome.deleted and change.source_file is not None:
        session.generation_status.pop(change.source_file, None)
    return outcome


def _find_change_and_test(
    session: AnalysisSession,
    change_path: Path,
    test_id: int,
) -> tuple[ProposedFileChange, ProposedTest]:
    resolved = change_path.resolve()
    change = next((c for c in session.proposals if c.path.resolve() == resolved), None)
    if change is None:
        raise RuntimeError("Proposal not found for this test file.")
    test = next((t for t in change.tests if t.id == test_id), None)
    if test is None:
        raise RuntimeError("Test not found.")
    return change, test
