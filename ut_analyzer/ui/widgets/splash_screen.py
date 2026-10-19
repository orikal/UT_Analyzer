"""First-launch initialization splash screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget


class InitializationSplash(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("UT Analyzer")
        self.setFixedSize(480, 200)
        self.setWindowFlags(
            Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)

        title = QLabel("UT Analyzer")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.stage_label = QLabel("Initializing...")
        self.stage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stage_label.setStyleSheet("color: #555; margin-top: 8px;")
        layout.addWidget(self.stage_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.setStyleSheet(
            "InitializationSplash { background: #ffffff; border: 1px solid #ccc; }"
        )

    def set_stage(self, message: str) -> None:
        self.stage_label.setText(message)

    def set_determinate(self, value: int, maximum: int = 100) -> None:
        self.progress.setRange(0, maximum)
        self.progress.setValue(value)

    def set_indeterminate(self) -> None:
        self.progress.setRange(0, 0)
