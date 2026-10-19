"""UT Analyzer entry point.

Run with:  python -m ut_analyzer.main
"""

from __future__ import annotations

import atexit
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from .adapters.base import load_default_adapters
from .core.generation.ai_runtime_manager import AIRuntimeManager, RuntimeState
from .ui.main_window import MainWindow
from .ui.widgets.settings_dialog import load_llm_settings
from .ui.widgets.splash_screen import InitializationSplash
from .utils.logging_config import setup_logging


def _shutdown_runtime() -> None:
    AIRuntimeManager.instance().shutdown()


def main() -> int:
    setup_logging()
    load_default_adapters()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("UT Analyzer")
    app.setOrganizationName("UTAnalyzer")

    splash = InitializationSplash()
    splash.show()
    app.processEvents()

    def on_stage(msg: str) -> None:
        splash.set_stage(msg)
        app.processEvents()

    settings = load_llm_settings()
    runtime = AIRuntimeManager.instance()
    status = runtime.initialize(settings, stage_callback=on_stage)

    splash.close()

    if status.compatibility and status.compatibility.has_warnings and not status.compatibility.ai_blocked:
        # Skip noisy warnings when AI runtime already initialized successfully.
        if status.state != RuntimeState.READY:
            warnings = [
                i.message for i in status.compatibility.issues if i.severity == "warning"
            ]
            if warnings:
                QMessageBox.warning(
                    None,
                    "System compatibility",
                    "Some recommended requirements are not met:\n\n"
                    + "\n".join(f"• {w}" for w in warnings)
                    + "\n\nAI test generation may be slower or unavailable. "
                    "Other features remain available.",
                )

    window = MainWindow(runtime_status=status)
    window.showMaximized()

    atexit.register(_shutdown_runtime)
    app.aboutToQuit.connect(_shutdown_runtime)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
