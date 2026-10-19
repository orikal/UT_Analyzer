"""Project / test directory selection plus the detected-environment summary."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...models import EnvironmentInfo


class ProjectSelector(QWidget):
    project_changed = Signal(str)
    test_dir_changed = Signal(str)
    scan_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        dirs_box = QGroupBox("Project")
        dirs_box.setStyleSheet(
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
        form = QFormLayout(dirs_box)
        self.project_edit = QLineEdit()
        self.project_edit.setPlaceholderText("Select the repository root directory...")
        self.project_edit.editingFinished.connect(
            lambda: self.project_changed.emit(self.project_edit.text())
        )
        project_browse = QPushButton("Browse...")
        project_browse.clicked.connect(self._browse_project)
        form.addRow(_bold_label("Project directory:"), _row(self.project_edit, project_browse))

        self.test_dir_edit = QLineEdit()
        self.test_dir_edit.setPlaceholderText("Optional - where new test files are created")
        self.test_dir_edit.editingFinished.connect(
            lambda: self.test_dir_changed.emit(self.test_dir_edit.text())
        )
        test_browse = QPushButton("Browse...")
        test_browse.clicked.connect(self._browse_test_dir)
        form.addRow(_bold_label("Unit-test directory:"), _row(self.test_dir_edit, test_browse))

        action_row = QWidget()
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self.scan_button = QPushButton("Scan && Analyze")
        self.scan_button.clicked.connect(self.scan_requested.emit)
        action_layout.addWidget(self.scan_button)
        action_layout.addStretch()
        form.addRow("", action_row)

        layout.addWidget(dirs_box)

        env_box = QGroupBox("Detected environment")
        env_box.setStyleSheet(
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
        self._env_form = QFormLayout(env_box)
        self._env_labels: dict[str, QLabel] = {}
        for label, _ in EnvironmentInfo().summary_rows():
            value = QLabel("—")
            value.setWordWrap(True)
            value.setStyleSheet("color: #888;")
            self._env_form.addRow(_bold_label(f"{label}:"), value)
            self._env_labels[label] = value
        self.ai_status = QLabel("AI: not configured")
        self.ai_status.setStyleSheet("color: #888;")
        self.ai_status.setWordWrap(True)
        self._env_form.addRow("", self.ai_status)
        layout.addWidget(env_box)

    # ---- API -----------------------------------------------------------------

    def project_path(self) -> Path | None:
        text = self.project_edit.text().strip()
        path = Path(text) if text else None
        return path if path and path.is_dir() else None

    def test_dir_path(self) -> Path | None:
        text = self.test_dir_edit.text().strip()
        path = Path(text) if text else None
        return path if path and path.is_dir() else None

    def set_environment(self, env: EnvironmentInfo) -> None:
        for label, value in env.summary_rows():
            widget = self._env_labels.get(label)
            if widget is None:
                continue
            widget.setText(value)
            widget.setStyleSheet("color: #888;" if value == "not detected" else "")

    def set_scan_enabled(self, enabled: bool) -> None:
        self.scan_button.setEnabled(enabled)

    def set_test_dir(self, path: str) -> None:
        self.test_dir_edit.setText(path)
        self.test_dir_changed.emit(path)

    def clear_test_dir(self) -> None:
        self.test_dir_edit.clear()
        self.test_dir_changed.emit("")

    def set_ai_status(
        self,
        available: bool,
        model: str,
        *,
        reason: str = "",
    ) -> None:
        if available:
            self.ai_status.setText(f"AI runtime: ready ({model})")
            self.ai_status.setStyleSheet("color: #22863a;")
            self.ai_status.setToolTip("")
        else:
            message = reason or "AI generation unavailable"
            suffix = f" (model: {model})" if model else ""
            self.ai_status.setText(f"AI runtime: {message}{suffix}")
            self.ai_status.setStyleSheet("color: #cb2431;")
            self.ai_status.setToolTip(message)

    # ---- browsing --------------------------------------------------------------

    def _browse_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select project directory")
        if path:
            self.project_edit.setText(path)
            self.project_changed.emit(path)

    def _browse_test_dir(self) -> None:
        start = self.project_edit.text() or ""
        path = QFileDialog.getExistingDirectory(self, "Select unit-test directory", start)
        if path:
            self.test_dir_edit.setText(path)
            self.test_dir_changed.emit(path)


def _row(*widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    return container


def _bold_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-weight: bold;")
    return label
