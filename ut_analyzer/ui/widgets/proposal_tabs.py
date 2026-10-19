"""One tab per proposed test file with per-test review cards."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...adapters.base import LanguageAdapter
from ...models import ProposedFileChange
from .test_card import TestCard


class FileChangeTab(QWidget):
    """Review tab for a single proposed file."""

    state_changed = Signal()

    def __init__(
        self,
        change: ProposedFileChange,
        adapter: LanguageAdapter,
        root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.change = change
        self.adapter = adapter
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        try:
            shown_path = change.path.relative_to(root)
        except ValueError:
            shown_path = change.path
        title = QLabel(
            f"<b>{shown_path}</b>"
            + (" <span style='color:#22863a'>(new file)</span>" if change.is_new_file else "")
            + (
                f" &nbsp;<span style='color:#666'>covers {change.source_file.name}</span>"
                if change.source_file
                else ""
            )
        )
        header.addWidget(title)
        header.addStretch(1)
        self.count_label = QLabel()
        self.count_label.setToolTip("Number of tests generated for this file")
        header.addWidget(self.count_label)
        approve_all = QPushButton("Approve all")
        approve_all.clicked.connect(self._approve_all)
        reject_all = QPushButton("Reject all")
        reject_all.clicked.connect(self._reject_all)
        header.addWidget(approve_all)
        header.addWidget(reject_all)
        layout.addLayout(header)

        cards_container = QWidget()
        self.cards_layout = QVBoxLayout(cards_container)
        self.cards: list[TestCard] = []
        for test in change.tests:
            card = TestCard(test, language=change.language)
            card.changed.connect(self._on_card_changed)
            self.cards_layout.addWidget(card)
            self.cards.append(card)
        self.cards_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_container)
        layout.addWidget(scroll, 1)
        self._update_count()

    def _update_count(self) -> None:
        total = len(self.change.tests)
        word = "test" if total == 1 else "tests"
        self.count_label.setText(
            f"<span style='color:#666'>{total} {word} generated</span>&nbsp;"
        )

    def append_tests(self, tests) -> None:
        """Add UI cards for tests (already merged into self.change.tests)."""
        stretch = self.cards_layout.takeAt(self.cards_layout.count() - 1)
        for test in tests:
            card = TestCard(test, language=self.change.language)
            card.changed.connect(self._on_card_changed)
            self.cards_layout.addWidget(card)
            self.cards.append(card)
        if stretch is not None:
            self.cards_layout.addItem(stretch)
        self._update_count()
        self.state_changed.emit()

    def refresh_cards(self) -> None:
        for card in self.cards:
            card.refresh()

    def _approve_all(self) -> None:
        self.change.approve_all()
        self.refresh_cards()
        self.state_changed.emit()

    def _reject_all(self) -> None:
        self.change.reject_all()
        self.refresh_cards()
        self.state_changed.emit()

    def _on_card_changed(self) -> None:
        self.state_changed.emit()
