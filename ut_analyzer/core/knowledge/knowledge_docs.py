"""Markdown knowledge pack written at the end of every scan.

The scan already learns everything needed to write correct unit tests: the
environment, the team's test conventions, the existing UT library (fixtures,
mocks, helpers, setup and construction recipes), every symbol declared in the
repository and which test file covers which source file.

This module persists all of it as Markdown files inside ``<root>/.ut_analyzer``
so a model (or a human) can read the whole picture in one place before writing a
test, instead of guessing:

- ``INDEX.md``            - entry point and the rules for writing a test here
- ``ENVIRONMENT.md``      - language, framework, build/test commands
- ``TEST_CONVENTIONS.md`` - naming, fixtures, assertions, example test
- ``UT_LIBRARY.md``       - what already exists in the UT folder and how it is used
- ``SOURCE_MAP.md``       - every file: what it declares, how to include it, who tests it
- ``REPO_KNOWLEDGE.md``   - full symbol index (written by the repository indexer)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ...models import (
    GAP_NO_TEST_FILE,
    EnvironmentInfo,
    QualityReport,
    RepoEntity,
    RepoKnowledge,
    ScanResult,
    TestConventions,
    UTKnowledge,
)
from ...utils.fs import read_text_safe, write_text_atomic
from .repo_indexer import KNOWLEDGE_FILE_NAME, ensure_knowledge_dir

INDEX_FILE_NAME = "INDEX.md"
ENVIRONMENT_FILE_NAME = "ENVIRONMENT.md"
CONVENTIONS_FILE_NAME = "TEST_CONVENTIONS.md"
UT_LIBRARY_FILE_NAME = "UT_LIBRARY.md"
SOURCE_MAP_FILE_NAME = "SOURCE_MAP.md"

MAX_MAPPED_FILES = 500
MAX_DECLARED_FILES = 400
MAX_SYMBOLS_PER_FILE = 40


@dataclass
class KnowledgeDoc:
    """One generated Markdown document."""

    name: str
    path: Path
    description: str


@dataclass
class KnowledgeDocs:
    """The Markdown documents produced by one scan."""

    folder: Path | None = None
    docs: list[KnowledgeDoc] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.docs

    def index_path(self) -> Path | None:
        for doc in self.docs:
            if doc.name == INDEX_FILE_NAME:
                return doc.path
        return None

    def summary(self) -> str:
        if self.folder is None or not self.docs:
            return "No knowledge files were written."
        return (
            f"Wrote {len(self.docs)} knowledge file(s) to {self.folder}: "
            + ", ".join(doc.name for doc in self.docs)
        )

    # ---- prompt ----------------------------------------------------------

    def prompt_text(self, seed_text: str = "", max_chars: int = 0) -> str:
        """The knowledge pack as prompt context.

        Sends the model the exact paths of the generated files (so a file-aware
        client can open them) plus, inlined, the source-map entries for the
        files this generation actually touches: their symbols and the exact
        include/import line each one needs.
        """
        if not self.docs:
            return ""
        header = "\n".join(
            [
                "Project knowledge files written by the scan of this repository. They "
                "are the ground truth for names, types, conventions and existing test "
                "helpers - if you are able to open files, read them before writing the "
                "tests:",
                *(f"- `{doc.path}` - {doc.description}" for doc in self.docs),
            ]
        )
        if max_chars and len(header) > max_chars:
            return ""
        remaining = max_chars - len(header) - 2 if max_chars else 0
        excerpt = self.source_map_excerpt(seed_text, remaining)
        return f"{header}\n\n{excerpt}" if excerpt else header

    def source_map_excerpt(self, seed_text: str, max_chars: int = 0) -> str:
        """The `SOURCE_MAP.md` sections describing the files *seed_text* uses."""
        doc = self._doc(SOURCE_MAP_FILE_NAME)
        if doc is None or not seed_text:
            return ""
        content = read_text_safe(doc.path)
        if not content:
            return ""
        wanted = {word.lower() for word in re.findall(r"[\w.]+", seed_text)}
        intro = (
            f"From `{SOURCE_MAP_FILE_NAME}` - the project files this test touches, the "
            "symbols they declare and the include/import line each one requires:"
        )
        blocks: list[str] = []
        used = len(intro)
        for block in content.split("\n### ")[1:]:
            name = block.split("`")[1] if "`" in block.split("\n", 1)[0] else ""
            if not name:
                continue
            file = Path(name)
            if file.name.lower() not in wanted and file.stem.lower() not in wanted:
                continue
            text = "### " + block.rstrip()
            cost = len(text) + 2
            if max_chars and used + cost > max_chars:
                break
            blocks.append(text)
            used += cost
        if not blocks:
            return ""
        return intro + "\n\n" + "\n\n".join(blocks)

    def _doc(self, name: str) -> KnowledgeDoc | None:
        for doc in self.docs:
            if doc.name == name:
                return doc
        return None


def write_knowledge_docs(
    root: Path,
    *,
    env: EnvironmentInfo | None = None,
    scan: ScanResult | None = None,
    quality: QualityReport | None = None,
    conventions: TestConventions | None = None,
    ut_knowledge: UTKnowledge | None = None,
    repo_knowledge: RepoKnowledge | None = None,
    test_dir: Path | None = None,
) -> KnowledgeDocs:
    """Write the full Markdown knowledge pack for the scanned repository."""
    result = KnowledgeDocs()
    folder = ensure_knowledge_dir(root)
    if folder is None:
        return result
    result.folder = folder

    language = env.language if env is not None else None
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    documents = [
        (
            ENVIRONMENT_FILE_NAME,
            "Language, test framework, build system and the commands that run the tests.",
            _environment_markdown(root, env, scan, quality, test_dir, generated_at),
        ),
        (
            CONVENTIONS_FILE_NAME,
            "How this team names, structures and asserts its tests, with a real example.",
            _conventions_markdown(conventions, generated_at),
        ),
        (
            UT_LIBRARY_FILE_NAME,
            "Fixtures, mocks, helpers and setup/construction recipes that already exist.",
            _ut_library_markdown(ut_knowledge, generated_at),
        ),
        (
            SOURCE_MAP_FILE_NAME,
            "Every source file: what it declares, how to include it, which test covers it.",
            _source_map_markdown(root, scan, quality, repo_knowledge, language, generated_at),
        ),
    ]

    written: list[KnowledgeDoc] = []
    for name, description, content in documents:
        target = folder / name
        try:
            write_text_atomic(target, content, backup=False)
        except OSError:
            continue
        written.append(KnowledgeDoc(name=name, path=target, description=description))

    if repo_knowledge is not None and repo_knowledge.markdown_path is not None:
        written.append(
            KnowledgeDoc(
                name=KNOWLEDGE_FILE_NAME,
                path=repo_knowledge.markdown_path,
                description=(
                    "Every type, enum value, constant, macro and function declared in "
                    "this repository, with initialization recipes."
                ),
            )
        )

    index = folder / INDEX_FILE_NAME
    try:
        write_text_atomic(index, _index_markdown(root, written, generated_at), backup=False)
        written.insert(
            0,
            KnowledgeDoc(
                name=INDEX_FILE_NAME,
                path=index,
                description="Start here: what the other files contain and the rules to follow.",
            ),
        )
    except OSError:
        pass

    result.docs = written
    return result


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _index_markdown(root: Path, docs: list[KnowledgeDoc], generated_at: str) -> str:
    lines = [
        f"# UT Analyzer knowledge pack - {root.name or root}",
        "",
        f"Generated automatically by UT Analyzer on {generated_at} by scanning "
        f"`{root}`.",
        "",
        "These files describe everything the tool learned about this repository. "
        "Read them before writing a unit test: they contain the real names, types, "
        "helpers and habits of this project, so a test written from them compiles and "
        "fits in with the existing suite.",
        "",
        "## Files",
        "",
    ]
    for doc in docs:
        lines.append(f"- [`{doc.name}`]({doc.name}) - {doc.description}")
    lines += [
        "",
        "## Suggested reading order",
        "",
        f"1. `{ENVIRONMENT_FILE_NAME}` - which language, framework and commands apply.",
        f"2. `{CONVENTIONS_FILE_NAME}` - the shape a test must have here.",
        f"3. `{UT_LIBRARY_FILE_NAME}` - what already exists and must be reused.",
        f"4. `{SOURCE_MAP_FILE_NAME}` - locate the file under test and its test file.",
        f"5. `{KNOWLEDGE_FILE_NAME}` - look up every symbol you are about to use.",
        "",
        "## Rules for writing a unit test in this project",
        "",
        "- Use only names, types, enum values, constants and signatures that appear in "
        f"`{KNOWLEDGE_FILE_NAME}` or in the source file under test. Never invent one.",
        "- Include or import the file listed next to a symbol before using that symbol.",
        f"- Reuse the fixtures, mocks, helpers and constants listed in `{UT_LIBRARY_FILE_NAME}` "
        "with their exact names; never redefine something that already exists.",
        "- Create and initialize objects the way this project already does (see the "
        f"setup and construction recipes in `{UT_LIBRARY_FILE_NAME}` and the initialization "
        f"snippets in `{KNOWLEDGE_FILE_NAME}`).",
        f"- Follow the naming, structure and assertion style in `{CONVENTIONS_FILE_NAME}`.",
        "- Respect access control: call public members directly, reach protected members "
        "only through a test class that inherits from the class under test, never touch "
        "private members.",
        "- Keep types exact: never assign a value whose type differs from the declared "
        "one and never initialize a non-pointer variable with null.",
        "- Implement every test fully: real setup, real calls, real assertions. A "
        "`TODO: <what is missing and what has to be done>` comment on the line directly "
        "above your best-guess line is allowed ONLY for a fact that is in none of these "
        "files - never as a substitute for code you could have written.",
        "",
        "This folder is regenerated on every scan and is excluded from version control.",
        "",
    ]
    return "\n".join(lines)


def _environment_markdown(
    root: Path,
    env: EnvironmentInfo | None,
    scan: ScanResult | None,
    quality: QualityReport | None,
    test_dir: Path | None,
    generated_at: str,
) -> str:
    lines = [
        "# Environment",
        "",
        f"Detected by UT Analyzer on {generated_at}.",
        "",
        "## Project",
        "",
        f"- Repository: `{root}`",
        f"- Unit-test folder: {_code_or_unknown(test_dir)}",
        "",
        "## Detected environment",
        "",
        "| Setting | Value |",
        "|---|---|",
    ]
    if env is not None:
        rows = [
            ("Language", env.language_name or env.language),
            ("Language id", env.language),
            ("Test framework", env.test_framework),
            ("Build system", env.build_system),
            ("Build command", env.build_command),
            ("Test command", env.test_command),
            ("Mocking libraries", ", ".join(env.mocking_libraries)),
        ]
    else:
        rows = [("Language", None)]
    for label, value in rows:
        lines.append(f"| {label} | {_cell(value) if value else '_not detected_'} |")

    if env is not None and env.notes:
        lines += ["", "## Notes from detection", ""]
        lines += [f"- {note}" for note in env.notes]

    if scan is not None:
        source_count = len([f for f in scan.files if not f.is_test])
        test_count = len([f for f in scan.files if f.is_test])
        lines += [
            "",
            "## What the scan found",
            "",
            f"- Files scanned: {len(scan.files)}",
            f"- Source files: {source_count}",
            f"- Test files: {test_count}",
        ]
    if quality is not None:
        lines += [
            f"- Test cases: {quality.total_test_cases}",
            f"- Tests without assertions: {quality.tests_without_assertions}",
            f"- Average assertions per test: {quality.avg_assertions_per_test}",
        ]
    lines += [
        "",
        "## Writing tests for this project",
        "",
        "- Write tests for the framework listed above and for no other one.",
        "- A new test must compile with the build system above without extra "
        "configuration: only use includes/imports that the project already uses.",
        "",
    ]
    return "\n".join(lines)


def _conventions_markdown(conventions: TestConventions | None, generated_at: str) -> str:
    lines = [
        "# Test conventions",
        "",
        f"Extracted from the existing unit tests on {generated_at}. A generated test "
        "must be indistinguishable from the tests already in this repository.",
        "",
        "## Conventions to follow",
        "",
    ]
    if conventions is None:
        lines.append("- No existing tests were available to learn from.")
        return "\n".join(lines) + "\n"

    lines.append(conventions.to_prompt_text())
    if conventions.indent:
        lines += ["", f"- Indentation: {len(conventions.indent)} space(s) per level."]
    if conventions.example_test:
        lines += [
            "",
            "## Representative existing test (match this style)",
            "",
            "```",
            conventions.example_test,
            "```",
        ]
    lines.append("")
    return "\n".join(lines)


def _ut_library_markdown(ut_knowledge: UTKnowledge | None, generated_at: str) -> str:
    lines = ["# Existing unit-test library", ""]
    if ut_knowledge is None or ut_knowledge.is_empty():
        lines += [
            f"Studied on {generated_at}: no reusable unit-test library was found.",
            "",
            "Write self-contained tests that follow `TEST_CONVENTIONS.md`.",
            "",
        ]
        return "\n".join(lines)

    folder = ut_knowledge.test_dir.name if ut_knowledge.test_dir is not None else "the UT folder"
    lines += [
        f"Learned on {generated_at} by reading every file in `{folder}`.",
        "",
        f"- {ut_knowledge.summary()}",
        "",
        "Reuse what is listed here - the exact fixtures, mocks, helpers and constants, "
        "with their exact names and signatures - instead of writing new ones, and build "
        "objects the way these snippets build them.",
        "",
        "## What the UT folder already provides",
        "",
        ut_knowledge.to_prompt_text(),
        "",
    ]
    return "\n".join(lines)


def _source_map_markdown(
    root: Path,
    scan: ScanResult | None,
    quality: QualityReport | None,
    repo_knowledge: RepoKnowledge | None,
    language: str | None,
    generated_at: str,
) -> str:
    lines = [
        "# Source map",
        "",
        f"Built on {generated_at}. It answers three questions for every file: what it "
        "declares, how to include/import it, and which unit-test file covers it.",
        "",
    ]

    gaps = {}
    if quality is not None:
        for gap in quality.gaps:
            gaps[_rel(gap.source_file, root)] = gap

    if scan is not None:
        sources = [item for item in scan.source_files(language or None)]
        lines += [
            f"## Test coverage map ({len(sources)} source file(s))",
            "",
            "| Source file | Unit-test file | Status | Functions that look untested |",
            "|---|---|---|---|",
        ]
        for item in sources[:MAX_MAPPED_FILES]:
            rel = _rel(item.path, root)
            gap = gaps.get(rel)
            if gap is None:
                lines.append(f"| `{_cell(rel)}` | - | not analyzed | - |")
                continue
            test_file = (
                f"`{_cell(_rel(gap.existing_test_file, root))}`"
                if gap.existing_test_file is not None
                else "-"
            )
            status = (
                "no unit tests yet"
                if gap.kind == GAP_NO_TEST_FILE
                else f"{gap.existing_test_count} existing test case(s)"
            )
            untested = ", ".join(gap.untested_functions[:8]) or "-"
            lines.append(f"| `{_cell(rel)}` | {test_file} | {status} | {_cell(untested)} |")
        if len(sources) > MAX_MAPPED_FILES:
            lines.append(f"| _...{len(sources) - MAX_MAPPED_FILES} more file(s)_ | | | |")
        lines.append("")

    if repo_knowledge is not None and not repo_knowledge.is_empty():
        by_file: dict[str, list[RepoEntity]] = {}
        includes: dict[str, str] = {}
        for entity in repo_knowledge.entities:
            key = entity.file or "unknown"
            by_file.setdefault(key, []).append(entity)
            if entity.include and key not in includes:
                includes[key] = entity.include
        lines += [
            f"## What each file declares ({len(by_file)} file(s))",
            "",
            "Look the file up here to know exactly which symbols it offers and which "
            "include/import line makes them available.",
            "",
        ]
        for position, (file_name, entities) in enumerate(sorted(by_file.items())):
            if position >= MAX_DECLARED_FILES:
                lines.append(f"_...{len(by_file) - MAX_DECLARED_FILES} more file(s)._")
                lines.append("")
                break
            lines.append(f"### `{file_name}`")
            lines.append("")
            include = includes.get(file_name)
            if include:
                lines.append(f"- Include/import: `{include}`")
            for entity in entities[:MAX_SYMBOLS_PER_FILE]:
                lines.append(f"- `{entity.name}` ({entity.kind})")
            if len(entities) > MAX_SYMBOLS_PER_FILE:
                lines.append(
                    f"- ... {len(entities) - MAX_SYMBOLS_PER_FILE} more symbol(s), "
                    f"see `{KNOWLEDGE_FILE_NAME}`"
                )
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rel(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _cell(value: object) -> str:
    """Table-cell safe text (pipes would break the Markdown table)."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _code_or_unknown(path: Path | None) -> str:
    return f"`{path}`" if path is not None else "_not detected_"
