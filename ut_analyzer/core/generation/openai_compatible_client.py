"""OpenAI-compatible chat-completions client for corporate / hosted LLM APIs."""

from __future__ import annotations

import json as jsonlib
import re
import time
from collections.abc import Callable

import requests

from ..cancellation import raise_if_cancelled
from .api_http import (
    build_auth_headers,
    build_request_kwargs,
    format_network_error,
    resolve_chat_completions_url,
    resolve_models_probe_url,
)
from .llm_errors import (
    ContextLengthError,
    LLMError,
    is_context_overflow,
    parse_context_limit,
)
from .llm_settings import (
    MIN_OUTPUT_TOKENS,
    LLMSettings,
    estimate_tokens,
    fit_output_budget,
    output_token_budget,
    remember_context_limit,
)
from .prompt_builder import response_has_tests
from .stream_sink import CHANNEL_CONTENT, CHANNEL_REASONING, StreamFn, stream_sink

# Reasoning models can burn the whole output budget on hidden tokens and return an
# empty message. Retry with a larger budget before giving up.
TRUNCATION_RETRIES = 2
MAX_OUTPUT_TOKEN_CEILING = 32768

# Statuses that usually mean "this gateway does not speak server-sent events".
_STREAM_UNSUPPORTED_STATUSES = frozenset({400, 404, 405, 422, 501})

# Extra request fields understood by vLLM / SGLang / Ollama-style gateways that
# switch the chat template out of "thinking" mode (Qwen3, DeepSeek-R1 distills...).
# They are dropped automatically when the gateway rejects them.
_THINKING_OFF_PARAMS: dict[str, object] = {
    "chat_template_kwargs": {"enable_thinking": False},
}
# In-prompt switch honoured by Qwen3-style templates; harmless for other models.
_NO_THINK_MARKER = "/no_think"

# Synthetic finish reason: the model produced only thinking, endlessly.
REASONING_LOOP = "reasoning_loop"

_THINK_OPEN = re.compile(r"<\s*(think|thinking|reasoning|reflection)\s*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(
    r"<\s*/\s*(think|thinking|reasoning|reflection)\s*>", re.IGNORECASE
)
_MAX_PARTIAL_TAG = 16


def normalize_api_base_url(base_url: str) -> str:
    """Return the resolved chat-completions URL (for display / diagnostics)."""
    return resolve_chat_completions_url(base_url)


class OpenAICompatibleClient:
    """Calls POST on the resolved chat-completions endpoint with Bearer authentication."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    @property
    def chat_completions_url(self) -> str:
        return resolve_chat_completions_url(self.settings.api_base_url)

    @property
    def base_url(self) -> str:
        return self.chat_completions_url

    def is_available(self) -> bool:
        has_auth = (
            not self.settings.requires_api_key() or bool(self.settings.api_key.strip())
        )
        return bool(self.chat_completions_url and has_auth and self.settings.model.strip())

    def list_models(self) -> list[str]:
        if not self.is_available():
            return []
        models_url = resolve_models_probe_url(self.settings.api_base_url)
        if models_url is None:
            return [self.settings.model]
        try:
            resp = requests.get(
                models_url,
                headers=build_auth_headers(self.settings, models_url),
                **build_request_kwargs(self.settings, min(10.0, self.settings.timeout), models_url),
            )
            if not resp.ok:
                return [self.settings.model]
            data = resp.json()
            models = [item.get("id", "") for item in data.get("data", []) if item.get("id")]
            return models or [self.settings.model]
        except requests.RequestException:
            return [self.settings.model]

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
                "Corporate API is not configured. Set base URL, API key, and model in Settings."
            )

        url = self.chat_completions_url
        req_kwargs = build_request_kwargs(self.settings, self.settings.timeout, url)
        headers = build_auth_headers(self.settings, url)

        token_key = "max_tokens"
        # Asking for more reply tokens than the window holds is rejected outright,
        # and so is asking for a reply that does not fit *next to this prompt*.
        window = self.settings.effective_context_tokens()
        budget = fit_output_budget(
            window, self.settings.max_output_tokens, len(prompt) + len(system)
        )
        ceiling = max(
            budget,
            output_token_budget(window, MAX_OUTPUT_TOKEN_CEILING),
        )
        reasoning_limit = self.settings.reasoning_char_limit()
        finish_reason = ""
        sink = stream_sink()
        use_stream = sink is not None
        no_thinking = bool(getattr(self.settings, "disable_thinking", True))
        extras_rejected = False
        shrunk_reply = False

        for _attempt in range(TRUNCATION_RETRIES + 1):
            payload: dict = {
                "model": self.settings.model,
                "messages": _build_messages(system, prompt, no_thinking=no_thinking),
                "temperature": self.settings.temperature,
                token_key: budget,
                "stream": use_stream,
            }
            if no_thinking and not extras_rejected:
                payload.update(_THINKING_OFF_PARAMS)

            def post(stream: bool, request: dict = payload) -> requests.Response:
                return _post_with_cancellation(
                    url,
                    headers=headers,
                    json=request,
                    req_kwargs=req_kwargs,
                    cancelled=cancelled,
                    progress=progress,
                    stream=stream,
                )

            try:
                resp = post(use_stream)
                if (
                    resp.status_code == 400
                    and token_key == "max_tokens"
                    and _rejects_max_tokens(resp)
                ):
                    # Newer gateways/models only accept "max_completion_tokens".
                    token_key = "max_completion_tokens"
                    payload.pop("max_tokens", None)
                    payload[token_key] = budget
                    resp = post(use_stream)
                if not resp.ok and not extras_rejected and _rejects_extra_params(resp):
                    # This gateway does not know the thinking-off fields; the
                    # in-prompt marker still applies.
                    extras_rejected = True
                    for key in _THINKING_OFF_PARAMS:
                        payload.pop(key, None)
                    resp = post(use_stream)
                if not resp.ok and not shrunk_reply:
                    smaller = self._reply_budget_that_fits(resp, prompt, system, budget)
                    if smaller is not None and smaller < budget:
                        # The prompt itself fits; only the reserved reply pushed
                        # the request over the window. Ask for a shorter reply
                        # instead of throwing the whole prompt away.
                        shrunk_reply = True
                        budget = smaller
                        payload[token_key] = budget
                        if progress is not None:
                            progress(
                                "Reply budget did not fit the model's context window; "
                                f"retrying with {budget} output tokens..."
                            )
                        resp = post(use_stream)
                if (
                    use_stream
                    and not resp.ok
                    and resp.status_code in _STREAM_UNSUPPORTED_STATUSES
                    and not is_context_overflow(resp.status_code, _safe_body(resp))
                ):
                    # The gateway refused server-sent events: drop the live view
                    # instead of failing the whole generation.
                    use_stream = False
                    payload["stream"] = False
                    resp = post(False)
            except requests.RequestException as exc:
                raise LLMError(
                    format_network_error(
                        exc,
                        url,
                        timeout=self.settings.timeout,
                        ssl_verify=self.settings.ssl_verify,
                    )
                ) from exc

            if use_stream and sink is not None:
                content, finish_reason = _read_streamed_content(
                    resp,
                    url,
                    sink=sink,
                    cancelled=cancelled,
                    reasoning_limit=reasoning_limit,
                )
            else:
                content, finish_reason = _extract_message_content(resp, url)
            if content.strip():
                return content
            if finish_reason == REASONING_LOOP:
                if no_thinking:
                    break
                # The model was allowed to think and never stopped: turn thinking
                # off and give it one more chance.
                no_thinking = True
                if progress is not None:
                    progress(
                        "The model kept thinking without writing tests; "
                        "retrying with thinking mode turned off..."
                    )
                continue
            if finish_reason != "length" or budget >= ceiling:
                break
            budget = min(budget * 2, ceiling)
            if progress is not None:
                progress(
                    "Model output was truncated before any test code; "
                    f"retrying with {budget} output tokens..."
                )

        raise LLMError(_empty_content_message(finish_reason, budget))

    def _reply_budget_that_fits(
        self, resp: requests.Response, prompt: str, system: str, budget: int
    ) -> int | None:
        """Reply budget to retry with when only the reply overflowed the window.

        Returns ``None`` when the response is not a context overflow, or when the
        prompt alone is already too large - the caller cannot fix that here, the
        prompt has to be rebuilt smaller.
        """
        body = _safe_body(resp)
        if not is_context_overflow(resp.status_code, body):
            return None
        limits = parse_context_limit(body)
        if limits.limit_tokens:
            remember_context_limit(self.settings.model, limits.limit_tokens)
        prompt_tokens = limits.prompt_tokens or estimate_tokens(len(prompt) + len(system))
        window = limits.limit_tokens or self.settings.effective_context_tokens()
        room = window - prompt_tokens - 256
        if room < MIN_OUTPUT_TOKENS:
            return None
        return min(budget, room)


def _build_messages(system: str, prompt: str, *, no_thinking: bool) -> list[dict[str, str]]:
    """Chat messages, with the in-prompt "do not think" switch when requested."""
    messages: list[dict[str, str]] = []
    if system:
        content = f"{system} {_NO_THINK_MARKER}" if no_thinking else system
        messages.append({"role": "system", "content": content})
    user = f"{prompt}\n\n{_NO_THINK_MARKER}" if no_thinking and not system else prompt
    messages.append({"role": "user", "content": user})
    return messages


def _rejects_max_tokens(resp: requests.Response) -> bool:
    body = _safe_body(resp).lower()
    return "max_tokens" in body and "max_completion_tokens" in body


def _rejects_extra_params(resp: requests.Response) -> bool:
    """True when the error body blames one of the thinking-off request fields."""
    body = _safe_body(resp).lower()
    return any(key.lower() in body for key in _THINKING_OFF_PARAMS)


def _safe_body(resp: requests.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - body decoding is best-effort
        return ""


def _extract_message_content(resp: requests.Response, url: str) -> tuple[str, str]:
    """Return ``(content, finish_reason)`` from a chat-completions response.

    Content may be missing (empty string) without being an error: reasoning models
    routinely return an empty message when they run out of output tokens.
    """
    _raise_for_status(resp, url)

    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMError("API returned an invalid response.") from exc
    return _content_from_body(body)


def _raise_for_status(resp: requests.Response, url: str) -> None:
    """Turn an unsuccessful chat-completions response into an ``LLMError``."""
    if resp.status_code == 401:
        raise LLMError("API authentication failed. Check your API key in Settings.")
    if resp.status_code == 404:
        raise LLMError(
            f"Chat completions endpoint not found at {url}. "
            "Copy the exact URL from VS Code Copilot (Manage Language Models)."
        )
    if not resp.ok:
        body = _safe_body(resp)
        if is_context_overflow(resp.status_code, body):
            limits = parse_context_limit(body)
            raise ContextLengthError(
                "The prompt did not fit into the model's context window "
                f"(HTTP {resp.status_code}): {body[:300]}",
                limits=limits,
            )
        raise LLMError(f"API returned HTTP {resp.status_code}: {body[:300]}")


def _content_from_body(body: object) -> tuple[str, str]:
    """Return ``(content, finish_reason)`` from a decoded chat-completions body."""
    if not isinstance(body, dict):
        raise LLMError("API returned an invalid response.")

    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise LLMError("API returned an empty response.")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = str(choice.get("finish_reason") or choice.get("stop_reason") or "")

    message = choice.get("message") or choice.get("delta") or {}
    content = ""
    thinking = ""
    if isinstance(message, dict):
        content, thinking = split_thinking(_coerce_text(message.get("content")))
        if not content.strip():
            # Some gateways expose the answer only via the reasoning channel.
            thinking = thinking or _coerce_text(message.get("reasoning_content"))
    if not content.strip():
        content, _ = split_thinking(_coerce_text(choice.get("text")))
    if not content.strip() and _looks_like_test_output(thinking):
        # Some gateways expose the whole answer through the reasoning channel.
        content = thinking
    if not content.strip() and thinking.strip() and not finish_reason:
        # Nothing but deliberation came back.
        finish_reason = REASONING_LOOP
    return content, finish_reason


def _looks_like_test_output(text: str) -> bool:
    """True when a reasoning blob actually carries the requested test blocks."""
    if "=== TEST:" in text or "```" in text:
        return True
    # Some gateways route the whole answer - plain, unfenced test code - through
    # the reasoning channel; the parser decides whether it is usable.
    return response_has_tests(text)


def split_thinking(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(answer, thinking)`` around ``<think>`` style blocks."""
    if not text:
        return "", ""
    splitter = _ThinkTagSplitter()
    chunks = splitter.feed(text) + splitter.flush()
    answer = "".join(part for channel, part in chunks if channel == CHANNEL_CONTENT)
    thinking = "".join(part for channel, part in chunks if channel == CHANNEL_REASONING)
    return answer, thinking


def _partial_tag_len(buffer: str) -> int:
    """Length of a trailing fragment that may still grow into a think tag."""
    tail = buffer[-_MAX_PARTIAL_TAG:]
    start = tail.rfind("<")
    if start < 0 or ">" in tail[start:]:
        return 0
    return len(tail) - start


class _ThinkTagSplitter:
    """Routes ``<think>...</think>`` spans of a token stream to the reasoning channel.

    Tags may arrive split across chunks, so an incomplete trailing fragment is
    held back until the next chunk completes it.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False

    @property
    def _channel(self) -> str:
        return CHANNEL_REASONING if self._thinking else CHANNEL_CONTENT

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._buffer += text
        while True:
            pattern = _THINK_CLOSE if self._thinking else _THINK_OPEN
            match = pattern.search(self._buffer)
            if match is None:
                break
            head = self._buffer[: match.start()]
            if head:
                out.append((self._channel, head))
            self._buffer = self._buffer[match.end() :]
            self._thinking = not self._thinking
        hold = _partial_tag_len(self._buffer)
        if len(self._buffer) > hold:
            cut = len(self._buffer) - hold
            emit, self._buffer = self._buffer[:cut], self._buffer[cut:]
            if emit:
                out.append((self._channel, emit))
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buffer:
            return []
        text, self._buffer = self._buffer, ""
        return [(self._channel, text)]


def _read_streamed_content(
    resp: requests.Response,
    url: str,
    *,
    sink: StreamFn,
    cancelled: Callable[[], bool] | None,
    reasoning_limit: int = 0,
) -> tuple[str, str]:
    """Consume a server-sent-event reply, publishing each chunk to ``sink``.

    Thinking is kept out of the returned answer: both the ``reasoning_content``
    channel and inline ``<think>`` blocks go to the live view only. When a model
    keeps deliberating past ``reasoning_limit`` characters without producing any
    answer text, the stream is abandoned and ``REASONING_LOOP`` is reported.
    """
    _raise_for_status(resp, url)

    emitter = _ThrottledEmitter(sink)
    splitter = _ThinkTagSplitter()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_chars = 0
    plain_lines: list[str] = []
    finish_reason = ""
    saw_event = False
    looping = False

    def publish(channel: str, text: str) -> None:
        nonlocal reasoning_chars
        if channel == CHANNEL_CONTENT:
            content_parts.append(text)
        else:
            reasoning_parts.append(text)
            reasoning_chars += len(text)
        emitter.add(channel, text)

    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            raise_if_cancelled(cancelled)
            line = (raw_line or "").strip()
            if not line:
                continue
            if not line.startswith("data:"):
                # A gateway that ignored "stream" sends a plain JSON body instead.
                plain_lines.append(line)
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                event = jsonlib.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            saw_event = True
            choices = event.get("choices") or []
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            reason = choice.get("finish_reason") or choice.get("stop_reason")
            if reason:
                finish_reason = str(reason)
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            thinking = _coerce_text(
                delta.get("reasoning_content") or delta.get("reasoning")
            )
            if thinking:
                publish(CHANNEL_REASONING, thinking)
            text = _coerce_text(delta.get("content"))
            if text:
                for channel, part in splitter.feed(text):
                    publish(channel, part)
            if (
                reasoning_limit
                and reasoning_chars > reasoning_limit
                and not "".join(content_parts).strip()
            ):
                looping = True
                break
    finally:
        for channel, part in splitter.flush():
            publish(channel, part)
        emitter.flush()
        resp.close()

    if not saw_event:
        try:
            body = jsonlib.loads("\n".join(plain_lines))
        except ValueError as exc:
            raise LLMError("API returned an invalid response.") from exc
        content, finish_reason = _content_from_body(body)
        if content:
            emitter.add(CHANNEL_CONTENT, content)
            emitter.flush()
        return content, finish_reason

    content = "".join(content_parts)
    if not content.strip():
        thinking = "".join(reasoning_parts)
        if _looks_like_test_output(thinking):
            # Some gateways expose the whole answer through the reasoning channel.
            return thinking, finish_reason
    if looping:
        return content, REASONING_LOOP
    if not content.strip() and reasoning_chars:
        finish_reason = finish_reason or REASONING_LOOP
    return content, finish_reason


class _ThrottledEmitter:
    """Batches streamed text so the UI gets a few updates per second, not per token."""

    def __init__(
        self,
        sink: StreamFn,
        *,
        min_chars: int = 160,
        min_interval: float = 0.15,
    ) -> None:
        self._sink = sink
        self._min_chars = min_chars
        self._min_interval = min_interval
        self._channel = ""
        self._buffer: list[str] = []
        self._size = 0
        self._last_flush = 0.0

    def add(self, channel: str, text: str) -> None:
        if channel != self._channel:
            self.flush()
            self._channel = channel
        self._buffer.append(text)
        self._size += len(text)
        if (
            self._size >= self._min_chars
            or time.monotonic() - self._last_flush >= self._min_interval
        ):
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._size = 0
        self._last_flush = time.monotonic()
        try:
            self._sink(self._channel, text)
        except Exception:  # noqa: BLE001 - a UI sink must never break generation
            pass


def _coerce_text(value: object) -> str:
    """Flatten OpenAI content values (string or list of content parts) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _empty_content_message(finish_reason: str, budget: int) -> str:
    if finish_reason == REASONING_LOOP:
        return (
            "The model only produced reasoning: it kept explaining what it intends "
            "to do (often repeating itself) and never wrote a test.\n\n"
            "What to try:\n"
            "- Use a non-thinking model, or the non-thinking variant of this one.\n"
            "- Lower 'Max tests per file' in Settings so the task is smaller.\n"
            "- Keep 'Disable model thinking' enabled in Settings (it is sent to the "
            "API, but some gateways ignore it)."
        )
    if finish_reason == "length":
        return (
            "API returned no text content: the model reached the output token limit "
            f"({budget}) before writing any tests. Try a smaller source file limit "
            "or a larger max output tokens value in Settings."
        )
    if finish_reason == "content_filter":
        return (
            "API returned no text content: the response was blocked by the "
            "provider's content filter."
        )
    suffix = f" (finish_reason={finish_reason})" if finish_reason else ""
    return f"API returned no text content{suffix}."


def _post_with_cancellation(
    url: str,
    *,
    headers: dict[str, str],
    json: dict,
    req_kwargs: dict,
    cancelled: Callable[[], bool] | None,
    progress: Callable[[str], None] | None,
    stream: bool = False,
) -> requests.Response:
    """Run an HTTP POST while polling for cooperative cancellation.

    With ``stream=True`` the response is returned as soon as the headers arrive; the
    caller is responsible for consuming (and closing) the body.
    """
    import threading

    result: list[requests.Response | BaseException] = []
    started = time.monotonic()
    last_progress = started

    def _request() -> None:
        try:
            result.append(
                requests.post(
                    url, headers=headers, json=json, stream=stream, **req_kwargs
                )
            )
        except BaseException as exc:  # noqa: BLE001 - propagate any request failure
            result.append(exc)

    thread = threading.Thread(target=_request, daemon=True)
    thread.start()
    while thread.is_alive():
        raise_if_cancelled(cancelled)
        if progress is not None:
            now = time.monotonic()
            if now - last_progress >= 5.0:
                elapsed = int(now - started)
                progress(f"Waiting for model response... {elapsed}s elapsed")
                last_progress = now
        thread.join(0.25)

    if not result:
        raise LLMError("The API request did not complete.")
    outcome = result[0]
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome
