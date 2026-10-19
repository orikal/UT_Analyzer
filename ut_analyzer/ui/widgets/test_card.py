"""A card widget for one proposed test: code editor, validation badge,
approve/reject buttons."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...models import ApprovalState, ProposedTest, ValidationStatus
from .compact_code_editor import (
    configure_compact_code_editor,
    monospace_font,
    resize_compact_code_editor,
)

_BADGE_COLORS = {
    ValidationStatus.NOT_RUN: "#888",
    ValidationStatus.VALIDATED: "#22863a",
    ValidationStatus.SYNTAX_ERROR: "#cb2431",
    ValidationStatus.COMPILE_ERROR: "#cb2431",
    ValidationStatus.TEST_FAILED: "#cb2431",
    ValidationStatus.DUPLICATE: "#b08800",
    ValidationStatus.NOT_VALIDATED: "#b08800",
}


class TestCard(QGroupBox):
    changed = Signal()

    def __init__(
        self,
        test: ProposedTest,
        parent: QWidget | None = None,
        *,
        language: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.test = test
        self.language = language
        self.setTitle(test.name)
        self.setStyleSheet(
            "QGroupBox {"
            " border: 1px solid #d0d7de;"
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
            "}"
        )
        layout = QVBoxLayout(self)

        if test.rationale or test.is_edge_case:
            rationale = QLabel(
                ("[edge case] " if test.is_edge_case else "") + test.rationale
            )
            rationale.setWordWrap(True)
            rationale.setStyleSheet("color: #555; font-style: italic;")
            layout.addWidget(rationale)

        self.badge = QLabel()
        self.badge.setWordWrap(True)
        layout.addWidget(self.badge)

        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setStyleSheet(
            "color:#666;font-family:Consolas,monospace;font-size:8pt;"
            "background:#f6f8fa;padding:4px;"
        )
        self.details.setVisible(False)
        layout.addWidget(self.details)

        self.editor = QPlainTextEdit()
        configure_compact_code_editor(
            self.editor,
            test.effective_code,
            monospace_font(),
            read_only=False,
            language=language,
        )
        self.editor.textChanged.connect(self._on_edited)
        layout.addWidget(self.editor)

        buttons = QHBoxLayout()
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setCheckable(True)
        self.approve_btn.clicked.connect(self._on_approve)
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setCheckable(True)
        self.reject_btn.clicked.connect(self._on_reject)
        buttons.addWidget(self.approve_btn)
        buttons.addWidget(self.reject_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.refresh()

    # ---- state sync ------------------------------------------------------------

    def refresh(self) -> None:
        result = self.test.validation
        badge_text = result.badge_text()
        self.badge.setText(badge_text)
        self.badge.setVisible(bool(badge_text))
        color = _BADGE_COLORS.get(result.status, "#888")
        self.badge.setStyleSheet(f"color: {color}; font-weight: bold;")
        if result.details:
            self.details.setText(result.details[-1500:])
            self.details.setVisible(True)
        else:
            self.details.setVisible(False)

        approved = self.test.approval is ApprovalState.APPROVED
        rejected = self.test.approval is ApprovalState.REJECTED
        self.approve_btn.setChecked(approved)
        self.reject_btn.setChecked(rejected)
        self.approve_btn.setStyleSheet(
            "background:#22863a;color:white;" if approved else ""
        )
        self.reject_btn.setStyleSheet(
            "background:#cb2431;color:white;" if rejected else ""
        )
        self.editor.setDisabled(rejected)

        if self.editor.toPlainText() != self.test.effective_code:
            self.editor.blockSignals(True)
            self.editor.setPlainText(self.test.effective_code)
            self.editor.blockSignals(False)
            resize_compact_code_editor(self.editor)

    def _on_edited(self) -> None:
        resize_compact_code_editor(self.editor)
        code = self.editor.toPlainText()
        self.test.edited_code = code if code != self.test.code else None
        self.changed.emit()

    def _on_approve(self) -> None:
        self.test.approval = (
            ApprovalState.APPROVED
            if self.test.approval is not ApprovalState.APPROVED
            else ApprovalState.PENDING
        )
        self.refresh()
        self.changed.emit()

    def _on_reject(self) -> None:
        self.test.approval = (
            ApprovalState.REJECTED
            if self.test.approval is not ApprovalState.REJECTED
            else ApprovalState.PENDING
        )
        self.refresh()
        self.changed.emit()
