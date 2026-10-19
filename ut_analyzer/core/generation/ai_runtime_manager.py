"""High-level AI runtime manager for corporate API connectivity."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..system.compatibility import CompatibilityReport, run_compatibility_checks
from .llm_client import check_api_health
from .llm_settings import LLMSettings

logger = logging.getLogger(__name__)


class RuntimeState(str, Enum):
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass
class RuntimeStatus:
    state: RuntimeState = RuntimeState.UNINITIALIZED
    message: str = ""
    technical_reason: str = ""
    endpoint: str = ""
    model: str = ""
    compatibility: CompatibilityReport | None = None
    init_stages: list[str] = field(default_factory=list)


class AIRuntimeManager:
    """Manages corporate API health checks and runtime status."""

    _instance: AIRuntimeManager | None = None

    def __init__(self) -> None:
        self._status = RuntimeStatus()
        self._settings: LLMSettings | None = None

    @classmethod
    def instance(cls) -> AIRuntimeManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    @property
    def settings(self) -> LLMSettings | None:
        return self._settings

    @property
    def is_ready(self) -> bool:
        return self._status.state == RuntimeState.READY

    def initialize(
        self,
        settings: LLMSettings,
        stage_callback: Callable[[str], None] | None = None,
    ) -> RuntimeStatus:
        def stage(msg: str) -> None:
            self._status.init_stages.append(msg)
            logger.info("Init stage: %s", msg)
            if stage_callback:
                stage_callback(msg)

        self._status = RuntimeStatus(state=RuntimeState.INITIALIZING, model=settings.model)
        self._settings = settings

        stage("Initializing application")
        stage("Checking system compatibility")
        compat = run_compatibility_checks()
        self._status.compatibility = compat

        if compat.ai_blocked:
            blocking = next(
                (i for i in compat.issues if i.severity == "error" and not i.can_continue),
                None,
            )
            reason = blocking.message if blocking else "Compatibility check failed"
            self._status.state = RuntimeState.FAILED
            self._status.message = "AI generation unavailable"
            self._status.technical_reason = reason
            logger.error("AI runtime blocked: %s", reason)
            return self._status

        stage("Connecting to AI API")
        from .api_http import resolve_chat_completions_url

        self._status.endpoint = resolve_chat_completions_url(settings.api_base_url)
        ok, health_msg = check_api_health(settings)
        if not ok:
            self._status.state = RuntimeState.FAILED
            self._status.message = "AI generation unavailable"
            self._status.technical_reason = health_msg
            logger.error("AI runtime error: %s", health_msg)
            stage("AI generation unavailable")
            return self._status

        self._status.state = RuntimeState.READY
        provider_label = "Corporate API"
        self._status.message = f"Ready ({provider_label})"
        self._status.technical_reason = health_msg
        self._settings = settings
        stage("Ready")
        logger.info(
            "%s ready at %s with model %s",
            provider_label,
            settings.api_base_url,
            settings.model,
        )
        return self._status

    def retry(
        self,
        settings: LLMSettings,
        stage_callback: Callable[[str], None] | None = None,
    ) -> RuntimeStatus:
        self.shutdown()
        return self.initialize(settings, stage_callback)

    def shutdown(self) -> None:
        if self._status.state == RuntimeState.READY:
            self._status.state = RuntimeState.UNINITIALIZED
        logger.info("AI runtime shutdown complete")

    def get_effective_settings(self) -> LLMSettings:
        if self._settings is None:
            return LLMSettings()
        return self._settings
