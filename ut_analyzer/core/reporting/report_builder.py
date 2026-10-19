"""Report generation: quality status report and before/after comparison, as HTML."""

from __future__ import annotations

import html

from ...models import (
    KIND_CLASS,
    KIND_CONSTANT,
    KIND_ENUM,
    KIND_FUNCTION,
    KIND_MACRO,
    KIND_STRUCT,
    BeforeAfterReport,
    EnvironmentInfo,
    ProjectSnapshot,
    QualityReport,
    RepoKnowledge,
    TestFile,
    ValidationStatus,
)
from ...models.proposals import ProposedFileChange
from ..knowledge.knowledge_docs import KnowledgeDocs


def make_snapshot(test_files: list[TestFile], quality: QualityReport | None) -> ProjectSnapshot:
    return ProjectSnapshot(
        test_file_count=len(test_files),
        test_case_count=sum(len(tf.cases) for tf in test_files),
        files_without_tests=list(quality.files_without_tests) if quality else [],
    )


def build_before_after(
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    changes: list[ProposedFileChange],
) -> BeforeAfterReport:
    tests = [t for c in changes for t in c.tests]
    report = BeforeAfterReport(before=before, after=after)
    report.generated_count = len(tests)
    report.approved_count = sum(1 for c in changes for t in c.approved_tests)
    report.rejected_count = sum(1 for c in changes for t in c.rejected_tests)
    report.edge_case_count = sum(1 for t in tests if t.is_edge_case)
    report.compiled_count = sum(
        1
        for t in tests
        if t.validation.status in (ValidationStatus.VALIDATED, ValidationStatus.TEST_FAILED)
    )
    report.passed_count = sum(1 for t in tests if t.validation.ok)
    report.failed_or_unvalidated = [
        (t.name, t.validation.badge_text())
        for t in tests
        if t.validation.status
        in (
            ValidationStatus.TEST_FAILED,
            ValidationStatus.COMPILE_ERROR,
            ValidationStatus.SYNTAX_ERROR,
            ValidationStatus.NOT_VALIDATED,
        )
    ]
    return report


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = (
    "<style>"
    "body{font-family:'Segoe UI',sans-serif;font-size:10pt;color:#24292e}"
    "h2{border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:18px}"
    "table{border-collapse:collapse;margin:6px 0}"
    "td,th{border:1px solid #ddd;padding:4px 10px;text-align:left}"
    "th{background:#f6f8fa}"
    ".warn{color:#b08800}.bad{color:#cb2431}.good{color:#22863a}.muted{color:#888}"
    "ul{margin:4px 0}"
    "</style>"
)


def _esc(value: object) -> str:
    return html.escape(str(value))


def render_quality_report_html(
    env: EnvironmentInfo,
    quality: QualityReport,
    repo_knowledge: RepoKnowledge | None = None,
    knowledge_docs: KnowledgeDocs | None = None,
) -> str:
    parts = [_CSS, "<h2>Environment</h2><table>"]
    for label, value in env.summary_rows():
        cls = "muted" if value == "not detected" else ""
        parts.append(f"<tr><th>{_esc(label)}</th><td class='{cls}'>{_esc(value)}</td></tr>")
    parts.append("</table>")
    if env.notes:
        parts.append("<ul>" + "".join(f"<li class='warn'>{_esc(n)}</li>" for n in env.notes) + "</ul>")

    parts.append("<h2>Test inventory</h2><table>")
    rows = [
        ("Source files", quality.total_source_files),
        ("Test files", quality.total_test_files),
        ("Test cases", quality.total_test_cases),
        ("Tests without assertions", quality.tests_without_assertions),
        ("Average assertions per test", quality.avg_assertions_per_test),
    ]
    for label, value in rows:
        parts.append(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>")
    parts.append("</table>")

    if repo_knowledge is not None and not repo_knowledge.is_empty():
        parts.append("<h2>Repository knowledge</h2><table>")
        for label, value in [
            ("Files indexed", repo_knowledge.indexed_files),
            (
                "Types (structs/classes)",
                len(repo_knowledge.of_kind(KIND_STRUCT))
                + len(repo_knowledge.of_kind(KIND_CLASS)),
            ),
            ("Enums", len(repo_knowledge.of_kind(KIND_ENUM))),
            (
                "Constants / macros",
                len(repo_knowledge.of_kind(KIND_CONSTANT))
                + len(repo_knowledge.of_kind(KIND_MACRO)),
            ),
            ("Free functions", len(repo_knowledge.of_kind(KIND_FUNCTION))),
            (
                "Types with a known initialization recipe",
                sum(1 for item in repo_knowledge.entities if item.init_examples),
            ),
        ]:
            parts.append(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>")
        parts.append("</table>")
        roles = repo_knowledge.by_role()
        if roles:
            parts.append(
                "<p>Collaborators found: "
                + _esc(
                    ", ".join(
                        f"{len(items)} {role}s" for role, items in list(roles.items())[:12]
                    )
                )
                + "</p>"
            )
        if repo_knowledge.markdown_path is not None:
            parts.append(
                "<p class='muted'>Every generation request reads this index. "
                f"Full details: {_esc(repo_knowledge.markdown_path)}</p>"
            )

    if knowledge_docs is not None and not knowledge_docs.is_empty():
        parts.append("<h2>Knowledge files</h2>")
        parts.append(
            "<p>The scan wrote everything it learned about this repository to "
            f"<code>{_esc(knowledge_docs.folder)}</code>. Open them (or point a model at "
            "them) to see the full picture behind every generated test.</p>"
        )
        parts.append("<table><tr><th>File</th><th>Contents</th></tr>")
        for doc in knowledge_docs.docs:
            parts.append(
                f"<tr><td><code>{_esc(doc.name)}</code></td>"
                f"<td>{_esc(doc.description)}</td></tr>"
            )
        parts.append("</table>")

    if quality.files_without_tests:
        parts.append(
            f"<h2>Files without tests ({len(quality.files_without_tests)})</h2><ul>"
        )
        parts.extend(f"<li>{_esc(f)}</li>" for f in quality.files_without_tests[:25])
        if len(quality.files_without_tests) > 25:
            parts.append(f"<li class='muted'>...and {len(quality.files_without_tests) - 25} more</li>")
        parts.append("</ul>")

    if quality.files_with_tests:
        parts.append(
            f"<h2>Files with tests that can get more cases ({len(quality.files_with_tests)})</h2><ul>"
        )
        parts.extend(f"<li>{_esc(f)}</li>" for f in quality.files_with_tests[:25])
        if len(quality.files_with_tests) > 25:
            parts.append(
                f"<li class='muted'>...and {len(quality.files_with_tests) - 25} more</li>"
            )
        parts.append("</ul>")

    if quality.trivial_tests:
        parts.append(f"<h2>Weak / trivial tests ({len(quality.trivial_tests)})</h2><ul>")
        parts.extend(f"<li class='warn'>{_esc(t)}</li>" for t in quality.trivial_tests[:20])
        parts.append("</ul>")

    for note in quality.notes:
        parts.append(f"<p class='warn'>{_esc(note)}</p>")
    return "".join(parts)


def render_before_after_html(report: BeforeAfterReport) -> str:
    parts = [_CSS, "<h2>Before / After</h2><table>"]
    parts.append("<tr><th></th><th>Before</th><th>After</th></tr>")
    rows = [
        ("Unit tests", report.before.test_case_count, report.after.test_case_count),
        ("Unit-test files", report.before.test_file_count, report.after.test_file_count),
        (
            "Files without tests",
            len(report.before.files_without_tests),
            len(report.after.files_without_tests),
        ),
    ]
    for label, before_value, after_value in rows:
        parts.append(
            f"<tr><th>{_esc(label)}</th><td>{_esc(before_value)}</td><td>{_esc(after_value)}</td></tr>"
        )
    parts.append("</table>")

    parts.append("<h2>Generation summary</h2><table>")
    for label, value in [
        ("Tests generated", report.generated_count),
        ("Tests approved", report.approved_count),
        ("Tests rejected", report.rejected_count),
        ("Edge-case tests added", report.edge_case_count),
        ("Tests that compiled", report.compiled_count),
        ("Tests that passed", report.passed_count),
    ]:
        parts.append(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>")
    parts.append("</table>")

    if report.failed_or_unvalidated:
        parts.append("<h2>Failed or not validated</h2><ul>")
        parts.extend(
            f"<li class='bad'><b>{_esc(name)}</b>: {_esc(reason)}</li>"
            for name, reason in report.failed_or_unvalidated
        )
        parts.append("</ul>")
    return "".join(parts)
