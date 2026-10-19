"""Tests for the Markdown knowledge pack written at the end of a scan."""

from __future__ import annotations

from pathlib import Path

from ut_analyzer.core.knowledge.knowledge_docs import (
    CONVENTIONS_FILE_NAME,
    ENVIRONMENT_FILE_NAME,
    INDEX_FILE_NAME,
    SOURCE_MAP_FILE_NAME,
    UT_LIBRARY_FILE_NAME,
    write_knowledge_docs,
)
from ut_analyzer.core.knowledge.repo_indexer import (
    KNOWLEDGE_FILE_NAME,
    build_repo_knowledge,
    write_knowledge_markdown,
)
from ut_analyzer.models import (
    GAP_NO_TEST_FILE,
    EnvironmentInfo,
    QualityGap,
    QualityReport,
    ScannedFile,
    ScanResult,
    TestConventions,
    UTCodeSample,
    UTKnowledge,
)


def _repo(root: Path) -> ScanResult:
    (root / "reader.py").write_text(
        "MAX_ITEMS = 8\n"
        "\n"
        "class Reader:\n"
        "    def read(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    return ScanResult(
        root=root,
        files=[ScannedFile(path=root / "reader.py", language="python")],
    )


def _docs_for(root: Path, scan: ScanResult):
    knowledge = build_repo_knowledge(scan, "python")
    write_knowledge_markdown(knowledge)
    quality = QualityReport(
        total_source_files=1,
        gaps=[
            QualityGap(
                source_file=root / "reader.py",
                reason="no test file covers this source file",
                untested_functions=["read"],
                existing_test_file=root / "tests" / "test_reader.py",
                kind=GAP_NO_TEST_FILE,
            )
        ],
    )
    conventions = TestConventions(
        file_naming="test_<name>.py",
        case_naming="snake_case, prefix test_",
        example_test="def test_read():\n    assert Reader().read() == 1\n",
    )
    ut_knowledge = UTKnowledge(
        test_dir=root / "tests",
        test_file_count=1,
        test_case_count=2,
        fixtures=["reader_fixture()"],
        construction_recipes=[
            UTCodeSample(title="Reader", code="reader = Reader()", file="test_reader.py")
        ],
    )
    return write_knowledge_docs(
        root,
        env=EnvironmentInfo(language="python", language_name="Python", test_framework="pytest"),
        scan=scan,
        quality=quality,
        conventions=conventions,
        ut_knowledge=ut_knowledge,
        repo_knowledge=knowledge,
        test_dir=root / "tests",
    )


def test_scan_writes_the_full_markdown_pack(tmp_path: Path) -> None:
    docs = _docs_for(tmp_path, _repo(tmp_path))

    assert docs.folder == tmp_path / ".ut_analyzer"
    names = [doc.name for doc in docs.docs]
    assert names[0] == INDEX_FILE_NAME
    for expected in (
        ENVIRONMENT_FILE_NAME,
        CONVENTIONS_FILE_NAME,
        UT_LIBRARY_FILE_NAME,
        SOURCE_MAP_FILE_NAME,
        KNOWLEDGE_FILE_NAME,
    ):
        assert expected in names
    for doc in docs.docs:
        assert doc.path.is_file()


def test_documents_contain_what_the_model_needs(tmp_path: Path) -> None:
    docs = _docs_for(tmp_path, _repo(tmp_path))
    folder = docs.folder
    assert folder is not None

    index = (folder / INDEX_FILE_NAME).read_text(encoding="utf-8")
    assert KNOWLEDGE_FILE_NAME in index
    assert "TODO:" in index

    environment = (folder / ENVIRONMENT_FILE_NAME).read_text(encoding="utf-8")
    assert "pytest" in environment
    assert "Python" in environment

    conventions = (folder / CONVENTIONS_FILE_NAME).read_text(encoding="utf-8")
    assert "test_<name>.py" in conventions
    assert "def test_read()" in conventions

    library = (folder / UT_LIBRARY_FILE_NAME).read_text(encoding="utf-8")
    assert "reader_fixture()" in library
    assert "reader = Reader()" in library

    source_map = (folder / SOURCE_MAP_FILE_NAME).read_text(encoding="utf-8")
    assert "reader.py" in source_map
    assert "test_reader.py" in source_map
    assert "no unit tests yet" in source_map
    assert "`Reader` (class)" in source_map


def test_prompt_text_points_the_model_at_the_files_and_inlines_the_map(
    tmp_path: Path,
) -> None:
    docs = _docs_for(tmp_path, _repo(tmp_path))

    text = docs.prompt_text(seed_text="import reader\nr = Reader()\n")

    assert str(tmp_path / ".ut_analyzer" / KNOWLEDGE_FILE_NAME) in text
    assert str(tmp_path / ".ut_analyzer" / SOURCE_MAP_FILE_NAME) in text
    # the source-map section for the file the test touches is inlined
    assert "### `reader.py`" in text
    assert "`Reader` (class)" in text


def test_prompt_text_respects_its_budget(tmp_path: Path) -> None:
    docs = _docs_for(tmp_path, _repo(tmp_path))

    assert docs.prompt_text(seed_text="import reader", max_chars=50) == ""

    full = docs.prompt_text(seed_text="import reader")
    trimmed = docs.prompt_text(seed_text="import reader", max_chars=len(full) - 100)
    assert 0 < len(trimmed) < len(full)
    assert "### `reader.py`" not in trimmed
