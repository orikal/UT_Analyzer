"""Native Ollama /api/chat client — faster and more reliable than OpenAI-compat for local models."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..cancellation import OperationCancelled, raise_if_cancelled
from .api_http import build_request_kwargs
from .generation_trace import trace
from .llm_errors import LLMError
from .llm_settings import (
    LLMSettings,
    OLLAMA_DEFAULT_BASE_URL,
    OLLAMA_DEFAULT_HOST,
    OLLAMA_DEFAULT_PORT,
)

logger = logging.getLogger(__name__)

OLLAMA_NUM_CTX = 4096
OLLAMA_IDLE_TIMEOUT = 120.0

_ollama_request_lock = threading.Lock()


def resolve_ollama_base_url(api_url: str) -> str:
    raw = (api_url or OLLAMA_DEFAULT_BASE_URL).strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or OLLAMA_DEFAULT_HOST).lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    # Ollama listens on the IPv4 loopback only. Windows resolves "localhost" to
    # ::1 first, which makes every request stall or fail, so pin it to 127.0.0.1.
    if host in {"localhost", "::1"}:
        host = OLLAMA_DEFAULT_HOST
    if port is None and host == OLLAMA_DEFAULT_HOST:
        port = OLLAMA_DEFAULT_PORT
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return f"{parsed.scheme}://{netloc}"


def resolve_ollama_chat_url(api_url: str) -> str:
    return f"{resolve_ollama_base_url(api_url)}/api/chat"


def resolve_ollama_tags_url(api_url: str) -> str:
    return f"{resolve_ollama_base_url(api_url)}/api/tags"


def is_ollama_installed() -> bool:
    """True when an Ollama executable exists on this machine."""
    if shutil.which("ollama"):
        return True
    roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")]
    candidates = [Path(root) / "Programs" / "Ollama" / "ollama.exe" for root in roots if root]
    candidates += [Path(root) / "Ollama" / "ollama.exe" for root in roots if root]
    candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    return any(path.exists() for path in candidates)


def ping_ollama(
    settings: LLMSettings, timeout: float = 5.0
) -> tuple[bool, list[str], str]:
    """Probe ``/api/tags``. Returns ``(server_reachable, installed_models, error)``.

    This is the only reliable liveness check: it needs no model to be pulled and
    never triggers a multi-second model load.
    """
    url = resolve_ollama_tags_url(settings.api_base_url)
    try:
        resp = requests.get(url, **build_request_kwargs(settings, timeout, url))
    except requests.RequestException as exc:
        return False, [], f"{type(exc).__name__}: {exc}"
    if not resp.ok:
        return False, [], f"HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return False, [], f"{url} did not return JSON (is this really Ollama?)"
    models = [
        str(item.get("name", ""))
        for item in data.get("models", [])
        if item.get("name")
    ]
    return True, models, ""


def list_ollama_models(settings: LLMSettings, timeout: float = 5.0) -> list[str]:
    """Return model names from a running Ollama instance."""
    _, models, _ = ping_ollama(settings, timeout)
    if models:
        return models
    return [settings.model] if settings.model.strip() else []


_CODE_MODEL_HINTS = ("coder", "code", "qwen", "deepseek", "llama", "mistral")


def pick_default_ollama_model(installed: list[str]) -> str:
    """Choose the most suitable installed model for test generation."""
    for hint in _CODE_MODEL_HINTS:
        for name in installed:
            if hint in name.lower():
                return name
    return installed[0] if installed else ""


def resolve_ollama_model_name(settings: LLMSettings) -> str:
    """Match configured model to an installed Ollama tag (e.g. add :latest)."""
    requested = settings.model.strip()
    installed = list_ollama_models(settings)
    if not requested:
        return pick_default_ollama_model(installed)
    if requested in installed:
        return requested
    if ":" not in requested:
        tagged = f"{requested}:latest"
        if tagged in installed:
            return tagged
    for name in installed:
        if name.split(":")[0] == requested.split(":")[0]:
            return name
    # The configured tag is not pulled; using an installed model beats failing.
    return pick_default_ollama_model(installed) or requested


class OllamaChatClient:
    """Calls Ollama's native ``/api/chat`` endpoint with bounded context size."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    @property
    def chat_url(self) -> str:
        return resolve_ollama_chat_url(self.settings.api_base_url)

    def is_available(self) -> bool:
        return bool(self.chat_url and self.settings.model.strip())

    def list_models(self) -> list[str]:
        if not self.is_available():
            return []
        return list_ollama_models(self.settings)

    def generate(
        self,
        prompt: str,
        system: str = "",
        *,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        if not self.is_available():
            raise LLMError(
                "Ollama is not configured. Set the local URL and model in Settings."
            )

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": resolve_ollama_model_name(self.settings),
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": OLLAMA_NUM_CTX,
                "num_predict": self.settings.max_output_tokens,
                "temperature": self.settings.temperature,
            },
        }

        url = self.chat_url
        total_timeout = self.settings.timeout
        connect_timeout = 15.0
        read_timeout = max(120.0, total_timeout)
        req_kwargs = build_request_kwargs(
            self.settings,
            (connect_timeout, read_timeout),
            url,
        )

        if progress is not None:
            progress(
                f"Starting Ollama (timeout {int(total_timeout)}s, loading model if needed)..."
            )
        trace(f"OLLAMA request start model={self.settings.model!r} url={url}")

        with _ollama_request_lock:
            return _stream_chat_with_timeout(
                url,
                payload=payload,
                req_kwargs=req_kwargs,
                cancelled=cancelled,
                progress=progress,
                total_timeout=total_timeout,
                idle_timeout=OLLAMA_IDLE_TIMEOUT,
            )


def _stream_chat_with_timeout(
    url: str,
    *,
    payload: dict,
    req_kwargs: dict,
    cancelled: Callable[[], bool] | None,
    progress: Callable[[str], None] | None,
    total_timeout: float,
    idle_timeout: float,
) -> str:
    """Stream Ollama chat with hard total timeout, idle timeout, and cancel abort."""
    result: list[str | BaseException] = []
    active_response: list[requests.Response | None] = [None]
    status_holder: list[str] = [""]
    last_activity: list[float] = [time.monotonic()]
    started = time.monotonic()
    limit = int(total_timeout)

    def _run() -> None:
        parts: list[str] = []
        saw_content = False
        try:
            trace("OLLAMA connecting...")
            with requests.post(url, json=payload, stream=True, **req_kwargs) as resp:
                active_response[0] = resp
                last_activity[0] = time.monotonic()
                trace(f"OLLAMA HTTP {resp.status_code}")
                if resp.status_code == 404:
                    raise LLMError(
                        f"Ollama chat endpoint not found at {url}. "
                        "Check that Ollama is running."
                    )
                if not resp.ok:
                    raise LLMError(
                        f"Ollama returned HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                try:
                    for raw_line in resp.iter_lines(decode_unicode=True):
                        if not raw_line:
                            continue
                        last_activity[0] = time.monotonic()
                        try:
                            chunk = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue

                        error = chunk.get("error")
                        if isinstance(error, str) and error.strip():
                            raise LLMError(f"Ollama error: {error.strip()}")

                        status = chunk.get("status")
                        if isinstance(status, str) and status.strip():
                            status_holder[0] = status.strip()
                            trace(f"OLLAMA status: {status.strip()}")
                            continue

                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            if not saw_content:
                                trace("OLLAMA first token received")
                            parts.append(content)
                            saw_content = True
                        if chunk.get("done"):
                            trace(f"OLLAMA done parts={len(parts)} chars={sum(len(p) for p in parts)}")
                            break
                except (AttributeError, ValueError) as exc:
                    trace(f"OLLAMA stream interrupted: {exc}")
                    if not parts:
                        raise LLMError(
                            "Ollama connection closed before any response was received. "
                            "Try again or check that the model is loaded."
                        ) from exc
                    trace(
                        f"OLLAMA using partial response "
                        f"parts={len(parts)} chars={sum(len(p) for p in parts)}"
                    )

            text = "".join(parts)
            if not text.strip():
                raise LLMError(
                    "Ollama returned an empty response. "
                    "Try a smaller source file or a different model."
                )
            result.append(text)
        except requests.RequestException as exc:
            trace(f"OLLAMA network error: {exc}")
            logger.warning("Ollama network error: %s", exc)
            result.append(exc)
        except LLMError as exc:
            trace(f"OLLAMA error: {exc}")
            logger.warning("Ollama error: %s", exc)
            result.append(exc)
        except Exception as exc:
            trace(f"OLLAMA unexpected error: {exc}")
            logger.exception("Ollama unexpected error")
            if parts:
                result.append("".join(parts))
            else:
                result.append(LLMError(f"Ollama request failed: {exc}"))
        finally:
            active_response[0] = None

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    last_progress = started

    while thread.is_alive():
        if cancelled is not None and cancelled():
            resp = active_response[0]
            if resp is not None:
                resp.close()
            raise OperationCancelled()
        raise_if_cancelled(cancelled)

        elapsed = time.monotonic() - started
        if elapsed > total_timeout:
            resp = active_response[0]
            if resp is not None:
                resp.close()
            trace(f"OLLAMA timeout after {int(elapsed)}s (limit {limit}s)")
            raise LLMError(
                f"Ollama did not finish within {limit}s. "
                "Try reducing 'Max tests per file' or use a smaller source file."
            )

        idle = time.monotonic() - last_activity[0]
        if idle > idle_timeout:
            resp = active_response[0]
            if resp is not None:
                resp.close()
            trace(f"OLLAMA idle for {int(idle)}s (limit {int(idle_timeout)}s)")
            raise LLMError(
                f"Ollama stopped responding for {int(idle)}s. "
                "Check that the Ollama service is still running and the model fits in memory."
            )

        if progress is not None and time.monotonic() - last_progress >= 2.0:
            status = status_holder[0]
            if status:
                progress(f"Ollama: {status} ({int(elapsed)}s / {limit}s)")
            else:
                progress(f"Ollama: {int(elapsed)}s / {limit}s")
            last_progress = time.monotonic()
        thread.join(0.25)

    if not result:
        raise LLMError("Ollama request did not complete.")
    outcome = result[0]
    if isinstance(outcome, BaseException):
        if isinstance(outcome, requests.RequestException):
            if not is_ollama_installed():
                raise LLMError(
                    "Ollama is not installed on this machine. Install it from "
                    "https://ollama.com/download, or switch to Corporate API in Settings."
                ) from outcome
            raise LLMError(
                f"Cannot reach Ollama at {url}. "
                "Make sure Ollama is running (`ollama serve`)."
            ) from outcome
        raise outcome
    return outcome
