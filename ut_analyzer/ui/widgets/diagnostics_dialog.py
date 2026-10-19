"""Diagnostic report dialog for troubleshooting AI runtime issues."""

from __future__ import annotations

import platform
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...core.generation.ai_runtime_manager import AIRuntimeManager
from ...core.generation.generation_trace import trace_path
from ...core.generation.llm_client import check_api_health, create_llm_client
from ...core.generation.llm_errors import LLMError
from ...ui.widgets.settings_dialog import load_llm_settings
from ...core.generation.api_http import resolve_chat_completions_url
from ...utils.paths import is_frozen, load_release_config, logs_dir


class DiagnosticsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnostics")
        self.setMinimumSize(640, 480)

        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(self._build_report())
        layout.addWidget(self.text)

        probe_row = QHBoxLayout()
        self.probe_status = QLabel("")
        probe_btn = QPushButton("Test API now")
        probe_btn.clicked.connect(self._run_probe)
        probe_row.addWidget(probe_btn)
        probe_row.addWidget(self.probe_status, 1)
        layout.addLayout(probe_row)

        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(self._copy)
        layout.addWidget(copy_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_report(self) -> str:
        config = load_release_config()
        runtime_mgr = AIRuntimeManager.instance()
        status = runtime_mgr.status
        llm = load_llm_settings()
        resolved_url = resolve_chat_completions_url(llm.api_base_url)
        app_cfg = config.get("application", {})

        lines = [
            "=== UT Analyzer Diagnostic Report ===",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            "",
            f"Application version: {app_cfg.get('version', __version__)}",
            f"Build date: {app_cfg.get('build_date', 'development')}",
            f"Running from: {'frozen EXE' if is_frozen() else 'Python source (run.py)'}",
            f"Windows: {platform.platform()}",
            f"Architecture: {platform.machine()}",
            "",
            "=== AI Runtime ===",
            "Provider: Corporate API (OpenAI-compatible)",
            f"State: {status.state.value}",
            f"Message: {status.message}",
            f"Technical reason: {status.technical_reason or 'none'}",
            f"Model: {llm.model or status.model or 'n/a'}",
            f"Timeout (effective): {int(llm.timeout)}s",
            f"Configured URL: {llm.api_base_url or 'n/a'}",
            f"Resolved endpoint: {resolved_url or status.endpoint or 'n/a'}",
            f"SSL verify: {'yes' if llm.ssl_verify else 'no (skip enabled)'}",
            f"Log directory: {logs_dir()}",
            f"Generation trace: {trace_path()}",
            "",
        ]

        if status.compatibility:
            lines.append("=== Compatibility ===")
            for issue in status.compatibility.issues:
                lines.append(f"[{issue.severity}] {issue.code}: {issue.message}")
            lines.append(
                f"Available RAM: {status.compatibility.available_ram_gb:.1f} GB"
            )
            lines.append(
                f"Available disk: {status.compatibility.available_disk_gb:.1f} GB"
            )
            lines.append("")

        lines.append("=== Init Stages ===")
        for stage in status.init_stages:
            lines.append(f"  - {stage}")

        lines.extend(["", "=== Recent Generation Trace ==="])
        lines.extend(self._tail_trace(max_lines=40))

        return "\n".join(lines)

    def _tail_trace(self, *, max_lines: int) -> list[str]:
        path = Path(trace_path())
        if not path.is_file():
            return ["  (no generation attempts logged yet)"]
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return [f"  (cannot read trace: {exc})"]
        if not content:
            return ["  (trace file is empty)"]
        tail = content[-max_lines:]
        return [f"  {line}" for line in tail]

    def _run_probe(self) -> None:
        self.probe_status.setText("Testing...")
        self.probe_status.repaint()
        llm = load_llm_settings()
        ok, health_msg = check_api_health(llm, timeout=15.0)
        if not ok:
            self.probe_status.setText(f"Health check failed: {health_msg}")
            self.text.setPlainText(self._build_report())
            return

        client = create_llm_client(llm)
        started = time.monotonic()
        try:
            reply = client.generate(
                "Reply with exactly one word: ok",
                cancelled=lambda: False,
                progress=None,
            )
            elapsed = time.monotonic() - started
            snippet = reply.strip().replace("\n", " ")[:80]
            self.probe_status.setText(
                f"OK in {elapsed:.1f}s — model replied: {snippet!r}"
            )
        except LLMError as exc:
            elapsed = time.monotonic() - started
            self.probe_status.setText(f"Failed after {elapsed:.1f}s: {exc}")
        except Exception as exc:  # noqa: BLE001 - show probe errors in UI
            elapsed = time.monotonic() - started
            self.probe_status.setText(f"Error after {elapsed:.1f}s: {exc}")

        self.text.setPlainText(self._build_report())

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
