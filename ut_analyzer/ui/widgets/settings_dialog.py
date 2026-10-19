"""Application settings dialog (corporate API and generation options)."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from ...core.generation.ai_runtime_manager import AIRuntimeManager
from ...core.generation.llm_settings import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMSettings,
    forget_context_limits,
    output_token_budget,
    prompt_char_budget,
    remember_context_limit,
    set_context_limit_observer,
)

_ORG, _APP = "UTAnalyzer", "UTAnalyzer"
_LEARNED_CONTEXT_GROUP = "llm/learned_context"


def _learned_key(model: str) -> str:
    """QSettings key for a model name (slashes would create nested groups)."""
    return f"{_LEARNED_CONTEXT_GROUP}/{model.strip().replace('/', '_')}"


def _persist_learned_limit(model: str, tokens: int) -> None:
    """Remember the window the gateway reported, so the next run starts right."""
    if not model.strip():
        return
    QSettings(_ORG, _APP).setValue(_learned_key(model), int(tokens))


set_context_limit_observer(_persist_learned_limit)


def load_llm_settings() -> LLMSettings:
    settings = QSettings(_ORG, _APP)
    model = str(settings.value("llm/model", "")).strip()
    learned = int(settings.value(_learned_key(model), 0) or 0)
    if learned:
        remember_context_limit(model, learned)
    return LLMSettings(
        model=model,
        timeout=float(settings.value("llm/timeout", 600.0)),
        api_base_url=str(settings.value("llm/api_base_url", "")).strip(),
        api_key=str(settings.value("llm/api_key", "")),
        ssl_verify=False,
        context_tokens=int(settings.value("llm/context_tokens", DEFAULT_CONTEXT_TOKENS)),
        max_output_tokens=int(
            settings.value("llm/max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
        ),
        disable_thinking=_as_bool(settings.value("llm/disable_thinking", True)),
    )


def _as_bool(value: object) -> bool:
    """QSettings returns booleans as strings on some backends."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def load_max_tests_per_file() -> int:
    return int(QSettings(_ORG, _APP).value("generation/max_tests_per_file", 5))


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        settings = QSettings(_ORG, _APP)
        form = QFormLayout(self)

        ai_group = QGroupBox("AI Provider")
        ai_form = QFormLayout(ai_group)

        self.api_base_url_edit = QLineEdit(str(settings.value("llm/api_base_url", "")))
        self.api_base_url_edit.setPlaceholderText(
            "https://internal-llm.company.com/v1/chat/completions"
        )
        self.api_base_url_edit.setToolTip(
            "Paste the exact URL from VS Code Copilot (Manage Language Models),\n"
            "or enter the server root (for example https://llm.company.com)."
        )
        ai_form.addRow("API URL:", self.api_base_url_edit)

        self.api_key_edit = QLineEdit(str(settings.value("llm/api_key", "")))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Paste your API key")
        self.api_key_label = QLabel("API key:")
        ai_form.addRow(self.api_key_label, self.api_key_edit)

        self.api_model_edit = QLineEdit(str(settings.value("llm/model", "")))
        self.api_model_edit.setPlaceholderText("Enter the exact model name used by your API")
        self.api_model_edit.setToolTip(
            "The model name is sent with every generation request.\n"
            "Use the exact identifier your API expects (for example the one shown in\n"
            "VS Code Copilot > Manage Language Models)."
        )
        ai_form.addRow("Model:", self.api_model_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 3600)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(int(float(settings.value("llm/timeout", 600.0))))
        ai_form.addRow("Generation timeout:", self.timeout_spin)

        runtime = AIRuntimeManager.instance()
        runtime_status = runtime.status
        status_label = QLabel(
            f"Status: {runtime_status.state.value}"
            + (f" — {runtime_status.message}" if runtime_status.message else "")
            + (f"\nModel in use: {runtime_status.model}" if runtime_status.model else "")
        )
        status_label.setWordWrap(True)
        ai_form.addRow("", status_label)

        form.addRow(ai_group)

        gen_group = QGroupBox("Generation")
        gen_form = QFormLayout(gen_group)

        self.max_tests_spin = QSpinBox()
        self.max_tests_spin.setRange(1, 20)
        self.max_tests_spin.setValue(
            int(settings.value("generation/max_tests_per_file", 5))
        )
        gen_form.addRow("Max tests per file:", self.max_tests_spin)

        self.context_tokens_spin = QSpinBox()
        self.context_tokens_spin.setRange(4, 2000)
        self.context_tokens_spin.setSuffix(" K tokens")
        self.context_tokens_spin.setSingleStep(8)
        self.context_tokens_spin.setValue(
            max(
                4,
                int(settings.value("llm/context_tokens", DEFAULT_CONTEXT_TOKENS)) // 1000,
            )
        )
        self.context_tokens_spin.setToolTip(
            "Context window of your model, in thousands of tokens.\n"
            "Everything the prompt may contain - the source under test, the API context,\n"
            "the repository knowledge and the learned unit-test library - is scaled from\n"
            "this value, so a large-context model receives much more project code.\n\n"
            "Set it to the window your model REALLY has. A value larger than the real\n"
            "window makes every request overflow, and no tests can be generated."
        )
        gen_form.addRow("Model context window:", self.context_tokens_spin)

        self.max_output_tokens_spin = QSpinBox()
        self.max_output_tokens_spin.setRange(512, 65536)
        self.max_output_tokens_spin.setSingleStep(512)
        self.max_output_tokens_spin.setValue(
            int(settings.value("llm/max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        )
        self.max_output_tokens_spin.setToolTip(
            "Upper bound for the reply. Raise it if generated tests get cut off.\n"
            "It is reserved inside the context window, so it can never exceed half of it."
        )
        gen_form.addRow("Max output tokens:", self.max_output_tokens_spin)

        self.disable_thinking_check = QCheckBox(
            "Ask the model not to think out loud"
        )
        self.disable_thinking_check.setChecked(
            _as_bool(settings.value("llm/disable_thinking", True))
        )
        self.disable_thinking_check.setToolTip(
            "Reasoning models often spend the whole reply on their own deliberation\n"
            "and never write a test. This turns the model's thinking mode off in the\n"
            "request and asks it to answer with the test blocks only.\n\n"
            "Uncheck it if your model produces better tests while thinking."
        )
        gen_form.addRow("Thinking:", self.disable_thinking_check)

        self.budget_label = QLabel()
        self.budget_label.setWordWrap(True)
        gen_form.addRow("", self.budget_label)
        self.context_tokens_spin.valueChanged.connect(self._update_budget_label)
        self.max_output_tokens_spin.valueChanged.connect(self._update_budget_label)
        self._update_budget_label()

        form.addRow(gen_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _update_budget_label(self) -> None:
        """Show what the two token settings actually mean for the prompt."""
        context_tokens = self.context_tokens_spin.value() * 1000
        requested = self.max_output_tokens_spin.value()
        reply = output_token_budget(context_tokens, requested)
        budget = prompt_char_budget(context_tokens, requested)
        text = (
            f"Prompt budget: ~{budget:,} characters "
            f"(~{budget // 3:,} tokens), reply: {reply:,} tokens."
        )
        if reply < requested:
            text += (
                f"\nMax output tokens is capped at {reply:,} because it must fit "
                "inside the context window."
            )
        self.budget_label.setText(text)

    def _save_and_close(self) -> None:
        settings = QSettings(_ORG, _APP)
        model = self.api_model_edit.text().strip()
        settings.setValue("llm/api_base_url", self.api_base_url_edit.text().strip())
        settings.setValue("llm/api_key", self.api_key_edit.text())
        settings.setValue("llm/model", model)
        settings.setValue("llm/skip_ssl_verify", True)
        settings.setValue("llm/timeout", float(self.timeout_spin.value()))
        settings.setValue(
            "llm/context_tokens", self.context_tokens_spin.value() * 1000
        )
        settings.setValue("llm/max_output_tokens", self.max_output_tokens_spin.value())
        settings.setValue(
            "llm/disable_thinking", self.disable_thinking_check.isChecked()
        )
        settings.setValue("generation/max_tests_per_file", self.max_tests_spin.value())
        # The user just told us which window to use: drop what was auto-learned so
        # the new value is honoured instead of an older, smaller one.
        settings.remove(_LEARNED_CONTEXT_GROUP)
        forget_context_limits()
        self.accept()
