"""Read-only HTML report view (analysis report / before-after report)."""

from __future__ import annotations

from PySide6.QtWidgets import QTextBrowser, QWidget


class ReportView(QTextBrowser):
    def __init__(self, placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        if placeholder:
            self.set_placeholder(placeholder)

    def set_placeholder(self, text: str) -> None:
        self.setHtml(
            f"<p style='color:#888;font-family:Segoe UI,sans-serif'>{text}</p>"
        )

    def set_report_html(self, html: str) -> None:
        self.setHtml(html)
