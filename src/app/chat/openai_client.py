"""OpenAI client — an LLMClient implementation (ADR-033).

Real integration with the OpenAI Python SDK (``AsyncOpenAI``) over the Chat Completions API
(function-calling + vision), NON-streaming — parity with the current non-streaming Anthropic path
(ADR-025 §non-streaming). Active only on instances with ``LLM_PROVIDER=openai``; the default
``anthropic`` path is unchanged.

All OpenAI-specific (de)serialization of the wire format lives INSIDE this client (ADR-033 §3):
- builds OpenAI Chat Completions ``messages`` from the neutral history (system message, assistant
  ``tool_calls``, ``role=tool`` with ``tool_call_id``) + first-turn attachments (image_url data-URI
  / text / native ``file`` content-part for PDF — ADR-041, closes TD-023);
- serializes tools to ``{type:function,function:{name(underscore),parameters}}``;
- parses the response: ``finish_reason`` → canonical stop_reason; ``message.tool_calls[]`` → domain
  tool_uses (reverse-mapped name, ``arguments`` JSON parsed to dict; invalid JSON / unknown name →
  ValidationFailedError); usage (cache_read from ``prompt_tokens_details.cached_tokens`` or 0,
  cache_write always 0); ``content_blocks`` = the normalized OpenAI assistant message for persist
  (so ``_build_messages`` can replay the continuation).

``cache_control`` is NOT applied (OpenAI auto-caches the prompt prefix; no explicit logic — ADR-033
§ Caching). The OpenAI key is never logged (redaction covers ``key``/``secret``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import openai
from openai.types.chat import ChatCompletionMessageFunctionToolCall

from app.chat.attachments import PreparedAttachments
from app.chat.llm_client import (
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    KeyValidation,
    LLMResult,
    LLMUsage,
    NeutralMessage,
)
from app.chat.tools import (
    UnknownToolNameError,
    openai_tool_function,
    to_domain_tool_name,
)
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import get_logger, log_event
from app.observability.metrics import llm_upstream_errors_total

_logger = get_logger("app.chat.openai")

_PROVIDER = "openai"

# Canonical stop_reason map (ADR-033 §2): OpenAI finish_reason → neutral value. Anything not
# tool_calls/length (stop, content_filter, None, …) collapses to end_turn (a final turn).
_FINISH_REASON_MAP: dict[str, str] = {
    "tool_calls": STOP_REASON_TOOL_USE,
    "length": STOP_REASON_MAX_TOKENS,
}


def _to_neutral_stop_reason(finish_reason: str | None) -> str:
    if finish_reason is None:
        return STOP_REASON_END_TURN
    return _FINISH_REASON_MAP.get(finish_reason, STOP_REASON_END_TURN)


def _log_upstream_error(exc: Exception, *, model: str, status_code: int | None) -> None:
    """Log ``llm_upstream_error`` BEFORE mapping (ADR-033 §10, mirrors the Anthropic path).

    Logs only non-sensitive metadata (status, exception class, model) — never the api-key or
    user-content. Level matrix mirrors TD-014: WARNING for 4xx, ERROR for 5xx / network errors.
    """
    if status_code is not None and 400 <= status_code < 500:
        level = logging.WARNING
    else:
        level = logging.ERROR
    fields: dict[str, Any] = {
        "event": "llm_upstream_error",
        "provider": _PROVIDER,
        "model": model,
        "exceptionClass": type(exc).__name__,
    }
    if status_code is not None:
        fields["status_code"] = status_code
    log_event(_logger, level, "llm_upstream_error", **fields)
    llm_upstream_errors_total.labels(
        provider=_PROVIDER,
        status_code=str(status_code) if status_code is not None else "none",
        error_type=type(exc).__name__,
    ).inc()


class OpenAIAuthError(Exception):
    """Raised when OpenAI rejects the (BYOK) key as unauthorized → key_status=invalid (ADR-016)."""


class OpenAIClient:
    """Async wrapper around ``openai.AsyncOpenAI``, implementing LLMClient (ADR-033).

    The service key is read from config; BYOK callers pass api_key per call. TLS verification is
    enabled by default by the SDK (httpx). No secrets are logged.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._default_model = settings.openai_model
        self._max_tokens = settings.openai_max_tokens
        self._service_key = settings.openai_api_key
        # One client per process; per-call key overrides via with_options(api_key=...).
        self._client = openai.AsyncOpenAI(
            api_key=self._service_key or "placeholder",
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )

    @staticmethod
    def _anthropic_blocks_to_openai_content(
        blocks: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Defensive cross-shape adapter (not used in production — one provider per instance).

        On an OpenAI instance the persisted blocks are already OpenAI-shaped, so this is only a
        guard for mixed/foreign blocks: collect text, drop the rest. Returns (text, tool_calls).
        """
        text_parts: list[str] = []
        for b in blocks:
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                text_parts.append(b["text"])
        return "".join(text_parts), []

    def _build_provider_messages(
        self,
        messages: list[NeutralMessage] | list[dict[str, Any]],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """Translate the neutral history into OpenAI Chat Completions messages (ADR-033 §3).

        The system prompt is the first ``role=system`` message (no cache_control — OpenAI
        auto-caches). user/assistant replay their persisted OpenAI wire content; a tool step becomes
        a ``role=tool`` message with ``tool_call_id`` = the raw provider id (call_..., ADR-008).
        Raw-dict items (external callers) are passed through unchanged.
        """
        out: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            if isinstance(msg, dict):
                out.append(msg)
                continue
            if msg.role == "assistant":
                out.append(self._assistant_message_from_blocks(msg.content_blocks))
            elif msg.role == "user":
                out.append(self._user_message_from_blocks(msg.content_blocks))
            elif msg.role == "tool":
                content = json.dumps(msg.error if msg.error is not None else msg.result)
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.provider_tool_use_id,
                        "content": content,
                    }
                )
        return out

    def _assistant_message_from_blocks(self, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Build an OpenAI assistant message from persisted blocks.

        On an OpenAI instance the persisted assistant block is already the normalized OpenAI
        assistant message ``{role:"assistant", content, tool_calls}`` (a single dict in the list).
        Replay it verbatim. As a guard for any foreign/text-only shape, fall back to text-only.
        """
        if len(blocks) == 1 and blocks[0].get("role") == "assistant":
            # Already the OpenAI assistant message (our persisted shape).
            msg = dict(blocks[0])
            msg.setdefault("content", None)
            return msg
        text, tool_calls = self._anthropic_blocks_to_openai_content(blocks)
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    def _user_message_from_blocks(self, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Build an OpenAI user message from persisted blocks.

        Persisted user content is text-block(s) + light placeholders (ADR-020 §3, provider-agnostic
        text). Concatenate the text into a single string content (OpenAI accepts a string or a
        content-part list; a string is simplest for replay of placeholders).
        """
        parts: list[str] = []
        for b in blocks:
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
        return {"role": "user", "content": "\n".join(parts)}

    @staticmethod
    def _serialize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serialize neutral tool definitions to the OpenAI function-tool format (ADR-033 §4).

        Delegates each neutral def to ``tools.openai_tool_function`` — the single source of truth
        for the OpenAI wire shape (in ``tools.py`` next to ``anthropic_tool_definitions``). The
        ``include_server_side`` gate was already applied by the orchestrator when building the
        neutral list (ADR-022 §A), so this only does per-item wire wrapping.
        """
        return [openai_tool_function(t) for t in tools]

    @staticmethod
    def _inject_attachments(
        messages: list[dict[str, Any]], attachments: PreparedAttachments
    ) -> None:
        """Inject first-turn attachment content parts into the last user message (ADR-020/§5).

        The OpenAI user content becomes a content-part list: the existing text part(s) followed by
        the attachment parts (image_url / text / native ``file`` content-part for PDF — ADR-041,
        closes TD-023). Mutates the last user message in place.
        """
        if not attachments.content_blocks:
            return
        for m in reversed(messages):
            if m.get("role") == "user":
                existing = m.get("content")
                parts: list[dict[str, Any]] = []
                if isinstance(existing, str):
                    if existing:
                        parts.append({"type": "text", "text": existing})
                elif isinstance(existing, list):
                    parts.extend(existing)
                parts.extend(attachments.content_blocks)
                m["content"] = parts
                return

    @staticmethod
    def _normalize_tool_call(
        tool_call: ChatCompletionMessageFunctionToolCall,
    ) -> dict[str, Any]:
        """Normalize one SDK tool_call to a clean wire dict for persist (ADR-021 per-provider)."""
        fn = tool_call.function
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {"name": fn.name, "arguments": fn.arguments},
        }

    def _parse_usage(self, completion: Any, model: str) -> LLMUsage:
        usage = getattr(completion, "usage", None)
        if usage is None:
            return LLMUsage(0, 0, model, 0, 0)
        cache_read = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0
        return LLMUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=model,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,  # OpenAI has no explicit cache-write count (auto-cache).
        )

    async def create_message(
        self,
        *,
        system_prompt: str,
        messages: list[NeutralMessage] | list[dict[str, Any]],
        tools: list[dict[str, Any]],
        attachments: PreparedAttachments | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> LLMResult:
        """Call chat.completions.create (non-streaming) and return a neutral LLMResult.

        model (ADR-034 §4): optional model id; None → the configured default
        (``settings.openai_model``) — current behavior, unchanged.
        """
        model = model if model is not None else self._default_model
        client = self._client
        if api_key is not None:
            client = client.with_options(api_key=api_key)

        wire_messages = self._build_provider_messages(messages, system_prompt)
        if attachments is not None:
            self._inject_attachments(wire_messages, attachments)
        openai_tools = self._serialize_tools(tools)

        try:
            completion = await client.chat.completions.create(
                model=model,
                max_tokens=self._max_tokens,
                messages=wire_messages,  # type: ignore[arg-type]
                tools=openai_tools or openai.NOT_GIVEN,  # type: ignore[arg-type]
            )
        except openai.AuthenticationError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", 401))
            raise OpenAIAuthError(str(exc)) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            _log_upstream_error(exc, model=model, status_code=None)
            raise UpstreamError("openai upstream error") from exc
        except openai.APIStatusError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", None))
            raise UpstreamError("openai upstream error") from exc

        choice = completion.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason

        text = message.content or ""
        # We only define function tools (ADR-033 §4); the SDK union may also carry "custom" tool
        # calls (no .function) — ignore them defensively (never produced for our tool set).
        raw_tool_calls: list[ChatCompletionMessageFunctionToolCall] = [
            tc
            for tc in (message.tool_calls or [])
            if isinstance(tc, ChatCompletionMessageFunctionToolCall)
        ]

        # content_blocks: the normalized OpenAI assistant message for persist/replay (single dict in
        # the list — the orchestrator stores content_blocks verbatim in chat_steps.payload).
        normalized_tool_calls = [self._normalize_tool_call(tc) for tc in raw_tool_calls]
        assistant_message: dict[str, Any] = {"role": "assistant", "content": message.content}
        if normalized_tool_calls:
            assistant_message["tool_calls"] = normalized_tool_calls
        content_blocks: list[dict[str, Any]] = [assistant_message]

        tool_uses: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            fn = tc.function
            try:
                domain_name = to_domain_tool_name(fn.name)
            except UnknownToolNameError as exc:
                # Upstream anomaly: an unmapped tool name must never surface as a valid tool.
                raise ValidationFailedError(str(exc)) from exc
            try:
                parsed_args = json.loads(fn.arguments) if fn.arguments else {}
            except (ValueError, json.JSONDecodeError) as exc:
                # Invalid JSON arguments → treated like an upstream anomaly (ADR-033 §4).
                raise ValidationFailedError(
                    f"invalid tool_call arguments JSON for {fn.name}"
                ) from exc
            if not isinstance(parsed_args, dict):
                raise ValidationFailedError(
                    f"tool_call arguments for {fn.name} must be a JSON object"
                )
            tool_uses.append({"id": tc.id, "name": domain_name, "input": parsed_args})

        return LLMResult(
            stop_reason=_to_neutral_stop_reason(finish_reason),
            content_blocks=content_blocks,
            usage=self._parse_usage(completion, model),
            text=text,
            tool_uses=tool_uses,
        )

    async def validate_key(self, api_key: str) -> KeyValidation:
        """Lightweight OpenAI call to validate a BYOK key (ADR-016, symmetric to Anthropic).

        Uses ``models.list`` (cheap, no generation). 401 → invalid; timeout/connection → offline;
        other status → offline; ok → valid. Never logs the key.
        """
        client = self._client.with_options(api_key=api_key)
        try:
            await client.models.list()
        except openai.AuthenticationError:
            return KeyValidation.invalid
        except (openai.APITimeoutError, openai.APIConnectionError, openai.APIStatusError):
            return KeyValidation.offline
        return KeyValidation.valid
