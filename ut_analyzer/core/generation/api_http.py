"""HTTP helpers for corporate OpenAI-compatible APIs (Copilot-style URL resolution)."""

from __future__ import annotations

import re
import urllib.request
from urllib.parse import urlparse, urlunparse

import requests

from .llm_settings import LLMSettings

_VERSION_SUFFIX = re.compile(r"/v\d+$", re.IGNORECASE)


def is_localhost_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    hostname = host.rsplit(":", 1)[0]
    return hostname in {"localhost", "127.0.0.1", "[::1]"}


def no_proxy_for_localhost(url: str) -> dict[str, str | None]:
    if is_localhost_url(url):
        return {"http": None, "https": None}
    return {}


def should_bypass_proxy(url: str) -> bool:
    """True when the host must be reached directly (localhost or proxy bypass list).

    Corporate LLM gateways are usually listed in ``no_proxy`` / Windows
    ``ProxyOverride``. Forcing those requests through the system proxy is a common
    cause of connection failures even when VS Code Copilot works.
    """
    if is_localhost_url(url):
        return True
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    try:
        return bool(urllib.request.proxy_bypass(hostname))
    except Exception:  # noqa: BLE001 - platform proxy lookup is best-effort
        return False


def resolve_chat_completions_url(api_url: str) -> str:
    """Resolve a user-entered URL to a chat-completions endpoint.

    Matches VS Code Copilot Custom Endpoint behaviour:
    - Full ``.../chat/completions`` URLs (with optional query string) are kept as-is.
    - ``https://host`` -> ``https://host/v1/chat/completions``
    - ``https://host/v1`` -> ``https://host/v1/chat/completions``
    - ``https://host/api`` -> ``https://host/api/v1/chat/completions``
    """
    url = api_url.strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if "/chat/completions" in path.lower():
        return url

    for suffix in ("/models", "/completions"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break

    if _VERSION_SUFFIX.search(path):
        resolved_path = f"{path}/chat/completions"
    elif path:
        resolved_path = f"{path}/v1/chat/completions"
    else:
        resolved_path = "/v1/chat/completions"

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            resolved_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def resolve_models_probe_url(api_url: str) -> str | None:
    """Optional ``/models`` probe URL; ``None`` when only a full completions URL is known."""
    if "/chat/completions" in api_url.lower():
        return None

    completions = resolve_chat_completions_url(api_url)
    parsed = urlparse(completions)
    if not parsed.path.lower().endswith("/chat/completions"):
        return None

    models_path = parsed.path[: -len("/chat/completions")] + "/models"
    return urlunparse(parsed._replace(path=models_path))


def build_auth_headers(settings: LLMSettings, url: str) -> dict[str, str]:
    """Build auth headers (Bearer by default; ``api-key`` for Azure-style hosts)."""
    key = settings.api_key.strip()
    host = urlparse(url).netloc.lower()
    headers = {"Content-Type": "application/json"}
    if not key:
        return headers
    if "azure" in host or ".openai.azure.com" in host:
        headers["api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def build_request_kwargs(
    settings: LLMSettings,
    timeout: float | tuple[float, float],
    url: str = "",
) -> dict:
    """Proxy + TLS options aligned with desktop tools on Windows."""
    kwargs: dict = {"timeout": timeout, "verify": settings.ssl_verify}
    if url and should_bypass_proxy(url):
        kwargs["proxies"] = {"http": None, "https": None}
        return kwargs
    proxies = urllib.request.getproxies()
    if proxies:
        kwargs["proxies"] = proxies
    return kwargs


def _contains_ssl_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.SSLError):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        for arg in exc.args:
            if isinstance(arg, requests.exceptions.SSLError):
                return True
            if isinstance(arg, Exception) and _contains_ssl_error(arg):
                return True
    cause = exc.__cause__
    return _contains_ssl_error(cause) if cause is not None else False


def format_network_error(
    exc: requests.RequestException,
    endpoint: str,
    *,
    timeout: float | None = None,
    ssl_verify: bool = True,
) -> str:
    """Turn a requests failure into a short, actionable message."""
    if isinstance(exc, requests.exceptions.Timeout):
        limit = timeout if timeout is not None else "the configured"
        return f"The API did not respond within {limit}s."

    if isinstance(exc, requests.exceptions.SSLError) or _contains_ssl_error(exc):
        hint = (
            " Enable 'Skip SSL verification' in Settings if your company uses an internal CA."
            if ssl_verify
            else ""
        )
        return (
            f"SSL verification failed for {endpoint}.{hint} "
            "Also verify the URL matches your VS Code Copilot configuration."
        )

    if isinstance(exc, requests.exceptions.ConnectionError):
        return (
            f"Cannot reach the API at {endpoint}. "
            "Paste the exact URL from VS Code Copilot (Manage Language Models), "
            "check VPN/network access, or enable 'Skip SSL verification' for internal CAs."
        )

    return f"Network error contacting the API at {endpoint}: {exc}"
