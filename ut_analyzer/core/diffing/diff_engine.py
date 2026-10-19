"""Diff computation and file-content composition for proposed changes."""

from __future__ import annotations

import difflib
import html

from ...adapters.base import LanguageAdapter
from ...models import ApprovalState, ProposedFileChange, ProposedTest


def compose_content(
    change: ProposedFileChange,
    adapter: LanguageAdapter,
    include: list[ProposedTest] | None = None,
    only_approved: bool = False,
) -> str:
    """Build the resulting file content for a proposal.

    ``include`` limits composition to specific tests (used for per-test
    validation); ``only_approved`` keeps only approved tests (used when
    applying). Otherwise all non-rejected tests are included.
    """
    if include is not None:
        tests = include
    elif only_approved:
        tests = change.approved_tests
    else:
        tests = [t for t in change.tests if t.approval is not ApprovalState.REJECTED]

    blocks = [t.effective_code.rstrip() for t in tests if t.effective_code.strip()]
    if not blocks:
        return change.original_content

    addition = "\n\n" + "\n\n".join(blocks) + "\n"

    if change.is_new_file and not change.original_content:
        base = change.preamble.rstrip("\n") + "\n"
        insert_at = adapter.insertion_point(base)
        return base[:insert_at].rstrip("\n") + addition + base[insert_at:]

    original = change.original_content
    insert_at = adapter.insertion_point(original)
    before = original[:insert_at].rstrip("\n")
    after = original[insert_at:]
    return before + addition + after


def unified_diff(change: ProposedFileChange, new_content: str) -> str:
    original_lines = change.original_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    name = change.path.name
    diff = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=f"a/{name}" + (" (new file)" if change.is_new_file else ""),
        tofile=f"b/{name}",
        lineterm="",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in diff)


def unified_diff_html(change: ProposedFileChange, new_content: str) -> str:
    """Unified diff rendered as colored HTML for a QTextBrowser."""
    text = unified_diff(change, new_content)
    if not text.strip():
        return "<p style='color:#888'>No changes.</p>"
    rows: list[str] = []
    for line in text.splitlines():
        escaped = html.escape(line) or "&nbsp;"
        if line.startswith("+") and not line.startswith("+++"):
            rows.append(f"<div style='background:#e6ffed;color:#22863a'>{escaped}</div>")
        elif line.startswith("-") and not line.startswith("---"):
            rows.append(f"<div style='background:#ffeef0;color:#cb2431'>{escaped}</div>")
        elif line.startswith("@@"):
            rows.append(f"<div style='background:#f1f8ff;color:#005cc5'>{escaped}</div>")
        else:
            rows.append(f"<div style='color:#24292e'>{escaped}</div>")
    return (
        "<div style='font-family:Consolas,monospace;font-size:9pt;white-space:pre'>"
        + "".join(rows)
        + "</div>"
    )


def side_by_side_html(change: ProposedFileChange, new_content: str) -> str:
    """Side-by-side diff rendered via difflib.HtmlDiff."""
    differ = difflib.HtmlDiff(wrapcolumn=90)
    table = differ.make_table(
        change.original_content.splitlines(),
        new_content.splitlines(),
        fromdesc="Existing" + (" (new file)" if change.is_new_file else ""),
        todesc="Proposed",
        context=True,
        numlines=3,
    )
    style = (
        "<style>"
        "table.diff{font-family:Consolas,monospace;font-size:8.5pt;border-collapse:collapse;width:100%}"
        "table.diff td,table.diff th{padding:1px 6px;vertical-align:top;white-space:pre-wrap}"
        ".diff_header{background:#f0f0f0;color:#666}"
        ".diff_next{display:none}"
        ".diff_add{background:#e6ffed}"
        ".diff_chg{background:#fff5b1}"
        ".diff_sub{background:#ffeef0}"
        "</style>"
    )
    return style + table
