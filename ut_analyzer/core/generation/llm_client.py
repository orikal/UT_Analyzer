"""LLM client factory and health checks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

import requests

from .api_http import (
    build_auth_headers,
    build_request_kwargs,
    format_network_error,
    resolve_chat_completions_url,
    resolve_models_probe_url,
)
from .llm_errors import LLMError
from .llm_settings import LLMSettings
from .openai_compatible_client import OpenAICompatibleClient


@runtime_checkable
class LLMClient(Protocol):
    def is_available(self) -> bool: ...

    def list_models(self) -> list[str]: ...

    def generate(
        self,
        prompt: str,
        system: str = "",
        *,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> str: ...


def create_llm_client(settings: LLMSettings) -> LLMClient:
    return OpenAICompatibleClient(settings)


def check_api_health(settings: LLMSettings, timeout: float = 10.0) -> tuple[bool, str]:
    """Validate API configuration and probe the chat-completions endpoint."""
    effective = settings
    if not effective.api_base_url.strip():
        return False, "API base URL is required"
    if effective.requires_api_key() and not effective.api_key.strip():
        return False, "API key is required"
    if not effective.model.strip():
        return False, "Model name is required"

    completions_url = resolve_chat_completions_url(effective.api_base_url)
    req_kwargs = build_request_kwargs(effective, timeout, completions_url)
    headers = build_auth_headers(effective, completions_url)

    models_url = resolve_models_probe_url(effective.api_base_url)
    if models_url is not None:
        try:
            resp = requests.get(models_url, headers=headers, **req_kwargs)
            if resp.status_code == 401:
                return False, "API authentication failed. Check your API key in Settings."
            if resp.ok:
                return True, f"OK ({completions_url})"
        except requests.RequestException:
            # Many internal gateways block GET /models; fall through to POST probe.
            pass

    return _probe_chat_completions(effective, completions_url, headers, req_kwargs)


def _probe_chat_completions(
    settings: LLMSettings,
    completions_url: str,
    headers: dict[str, str],
    req_kwargs: dict,
) -> tuple[bool, str]:
    """Verify the chat-completions endpoint the same way Copilot calls it."""
    payload = {
        "model": settings.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    try:
        resp = requests.post(
            completions_url,
            headers=headers,
            json=payload,
            **req_kwargs,
        )
        if resp.status_code == 401:
            return False, "API authentication failed. Check your API key in Settings."
        if resp.status_code == 403:
            return False, (
                "API rejected the request (HTTP 403). "
                "Check that your API key is authorised for this model."
            )
        if resp.status_code == 404:
            return False, (
                f"Chat completions endpoint not found at {completions_url}. "
                "Copy the exact URL from VS Code Copilot (Manage Language Models)."
            )
        if resp.ok:
            return True, f"OK ({completions_url})"
        if resp.status_code in (400, 422):
            # The gateway is reachable and authenticated; it only disliked the tiny
            # probe payload (e.g. requires max_completion_tokens). Do not block AI.
            return True, (
                f"Endpoint reachable at {completions_url}, but rejected the probe "
                f"payload (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        return False, f"API returned HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as exc:
        return False, format_network_error(
            exc,
            completions_url,
            timeout=req_kwargs.get("timeout"),
            ssl_verify=settings.ssl_verify,
        )
