"""About dialog showing application and runtime version information."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core.generation.ai_runtime_manager import AIRuntimeManager
from ...utils.paths import load_release_config


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About UT Analyzer")
        self.setMinimumWidth(420)

        config = load_release_config()
        app_cfg = config.get("application", {})
        runtime = AIRuntimeManager.instance().status

        build_date = app_cfg.get("build_date") or "development"
        app_version = app_cfg.get("version", __version__)

        lines = [
            f"<h2>UT Analyzer</h2>",
            f"<p>Version {app_version}<br>Build: {build_date}</p>",
            f"<p><b>AI Runtime</b><br>"
            f"Provider: Corporate API<br>"
            f"Model: {runtime.model or 'not configured'}<br>"
            f"Status: {runtime.state.value}<br>"
            f"Endpoint: {runtime.endpoint or 'n/a'}</p>",
            "<p>Desktop unit-test analysis and generation for multi-language projects.</p>",
            "<p>Test generation uses your company's internal LLM API.</p>",
            "<p>See <i>licenses/</i> in the installation directory for third-party notices.</p>",
        ]

        layout = QVBoxLayout(self)
        label = QLabel("".join(lines))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
