"""The saved analysis in ``.ut_analyzer`` must round-trip into a usable session."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.core.knowledge.knowledge_docs import KnowledgeDoc, KnowledgeDocs
from ut_analyzer.core.knowledge.session_cache import (
    CACHE_FILE_NAME,
    CacheError,
    find_cached_analysis,
    load_analysis,
    save_analysis,
)
from ut_analyzer.core.orchestrator import AnalysisSession, has_saved_analysis, run_analyze_cached
from ut_analyzer.models import (
    EnvironmentInfo,
    FixtureLifecycle,
    ProjectConfig,
    QualityGap,
    QualityReport,
    RepoEntity,
    RepoKnowledge,
    RepoUsage,
    ScannedFile,
    ScanResult,
    TestCase,
    TestConventions,
    TestFile,
    UTKnowledge,
)


def _session(root: Path) -> AnalysisSession:
    session = AnalysisSession(config=ProjectConfig(root=root, test_dir=root / "tests"))
    session.scan = ScanResult(
        root=root,
        files=[
            ScannedFile(path=root / "src" / "alpha.cpp", language="cpp", size=12),
            ScannedFile(path=root / "tests" / "alphaUT.cpp", language="cpp", is_test=True),
        ],
    )
    session.env = EnvironmentInfo(language="cpp", test_framework="gtest", notes=["n"])
    session.test_files = [
        TestFile(
            path=root / "tests" / "alphaUT.cpp",
            language="cpp",
            framework="gtest",
            cases=[TestCase(name="Works", file=root / "tests" / "alphaUT.cpp", line=3)],
            content="TEST(A, Works) {}",
        )
    ]
    session.conventions = TestConventions(file_naming="<name>UT.cpp", uses_fixtures=True)
    session.repo_knowledge = RepoKnowledge(
        root=root,
        language="cpp",
        entities=[
            RepoEntity(
                kind="class",
                name="Alpha",
                members=["void run()"],
                init_examples=[RepoUsage(file="src/alpha.cpp", code="Alpha a;")],
            )
        ],
        declarations={str(root / "src" / "alpha.cpp"): "class Alpha { void run(); };"},
    )
    session.ut_knowledge = UTKnowledge(
        test_dir=root / "tests",
        test_file_count=1,
        lifecycle=FixtureLifecycle(setup_files=1, files_with_hooks=1, init_methods=("Init",)),
    )
    session.knowledge_docs = KnowledgeDocs(
        folder=root / ".ut_analyzer",
        docs=[KnowledgeDoc("INDEX.md", root / ".ut_analyzer" / "INDEX.md", "entry point")],
    )
    session.quality = QualityReport(
        total_source_files=1,
        gaps=[
            QualityGap(
                source_file=root / "src" / "alpha.cpp",
                reason="no test file",
                untested_functions=["run"],
                existing_test_file=root / "tests" / "alphaUT.cpp",
            ),
            QualityGap(
                source_file=root / "other" / "beta.cpp",
                reason="no test file",
                untested_functions=["go"],
            ),
        ],
    )
    return session


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    saved = save_analysis(_session(root))

    assert saved == root / ".ut_analyzer" / CACHE_FILE_NAME
    assert has_saved_analysis(root)
    assert find_cached_analysis(root) == saved

    stored = load_analysis(root)
    env = stored["env"]
    scan = stored["scan"]
    quality = stored["quality"]
    knowledge = stored["repo_knowledge"]
    ut = stored["ut_knowledge"]

    assert isinstance(env, EnvironmentInfo) and env.test_framework == "gtest"
    assert isinstance(scan, ScanResult) and scan.root == root
    assert isinstance(scan.files[0].path, Path) and scan.files[1].is_test is True
    assert stored["test_files"][0].cases[0].name == "Works"
    assert stored["conventions"].file_naming == "<name>UT.cpp"
    assert knowledge.entities[0].init_examples[0].code == "Alpha a;"
    assert knowledge.declarations[str(root / "src" / "alpha.cpp")].startswith("class Alpha")
    assert ut.lifecycle.init_methods == ("Init",)
    assert isinstance(ut.test_dir, Path)
    assert stored["knowledge_docs"].index_path() == root / ".ut_analyzer" / "INDEX.md"
    assert isinstance(quality.gaps[0].existing_test_file, Path)
    assert quality.gaps[0].untested_functions == ["run"]


def test_run_analyze_cached_reuses_the_saved_analysis(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    save_analysis(_session(root))

    messages: list[str] = []
    session = run_analyze_cached(
        ProjectConfig(root=root, included_dirs=[root], focus_dirs=[root / "src"]),
        messages.append,
        lambda: False,
    )

    assert session.adapter is not None and session.adapter.language_id == "cpp"
    assert session.quality is not None
    # The gap outside the marked folder is filtered out, nothing is re-scanned.
    assert [gap.source_file for gap in session.quality.gaps] == [root / "src" / "alpha.cpp"]
    assert session.before_snapshot is not None
    assert session.approval is not None
    assert any("Reusing the analysis" in message for message in messages)


def test_missing_cache_raises_cache_error(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert not has_saved_analysis(root)
    try:
        load_analysis(root)
    except CacheError as exc:
        assert "Scan & Analyze" in str(exc)
    else:  # pragma: no cover - the call must fail
        raise AssertionError("load_analysis should raise CacheError")
