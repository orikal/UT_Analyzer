"""Shared read-only code editor sizing for test preview panels."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QPlainTextEdit, QSizePolicy

from .code_highlighter import attach_code_highlighter


def compact_code_editor_height(editor: QPlainTextEdit, code: str) -> int:
    line_count = max(1, len(code.splitlines()))
    line_px = editor.fontMetrics().lineSpacing()
    return line_px * (line_count + 1) + editor.frameWidth() * 2


def configure_compact_code_editor(
    editor: QPlainTextEdit,
    code: str,
    font: QFont,
    *,
    read_only: bool = True,
    language: str | None = None,
) -> None:
    """Code box sized to ``len(lines) + 1`` visible rows.

    When ``language`` maps to a supported dialect the code is syntax highlighted.
    """
    # The highlighter is parented to the document, so it stays alive with the editor.
    attach_code_highlighter(editor, language)
    editor.setPlainText(code)
    editor.setFont(font)
    editor.setReadOnly(read_only)
    editor.setFrameShape(QFrame.Shape.StyledPanel)
    editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    editor.document().setDocumentMargin(0)
    editor.setViewportMargins(0, 0, 0, 0)
    editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    editor.setFixedHeight(compact_code_editor_height(editor, code))


def resize_compact_code_editor(editor: QPlainTextEdit, code: str | None = None) -> None:
    """Update height after the editor text changes."""
    text = editor.toPlainText() if code is None else code
    editor.setFixedHeight(compact_code_editor_height(editor, text))


def monospace_font(*, point_size: int = 9) -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(point_size)
    return font
