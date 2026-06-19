"""Unit tests for the OpenAI LLMClient implementation and the provider factory (ADR-033).

No real OpenAI calls: the SDK async client (``chat.completions.create`` / ``models.list``) is
replaced with an in-memory fake, and the response is a lightweight object the client reads by
attribute (parity with the real ``ChatCompletion`` shape). Covers the OpenAI-specific seam:
finish_reason→canonical stop_reason, tool_calls→domain tool_uses (reverse-map + JSON arg parse +
invalid-JSON rejection), attachment mapping (image_url / text / PDF→native file-input, ADR-041),
tool-definition wire format + server-side gating, validate_key outcomes,
usage (cached_tokens→cache_read), and the
``get_llm_client()`` factory dispatch by ``LLM_PROVIDER``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletionMessageFunctionToolCall
from openai.types.chat.chat_completion_message_function_tool_call import Function

from app.chat.attachments import PreparedAttachments, prepare_attachments
from app.chat.llm_client import (
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    KeyValidation,
    NeutralMessage,
    get_llm_client,
)
from app.chat.openai_client import OpenAIAuthError, OpenAIClient
from app.chat.tools import neutral_tool_definitions
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.schemas.chat import AttachmentIn

# --------------------------------------------------------------------------------------------
# Fakes for the OpenAI SDK async client. The client under test only touches
# ``chat.completions.create(...)`` and ``models.list()``; everything else is unused.
# --------------------------------------------------------------------------------------------


def _function_tool_call(
    call_id: str, name: str, arguments: str
) -> ChatCompletionMessageFunctionToolCall:
    return ChatCompletionMessageFunctionToolCall(
        id=call_id, type="function", function=Function(name=name, arguments=arguments)
    )


def _completion(
    *,
    content: str | None = "hi",
    finish_reason: str = "stop",
    tool_calls: list[ChatCompletionMessageFunctionToolCall] | None = None,
    usage: Any | None = None,
) -> SimpleNamespace:
    """A minimal stand-in for the SDK ChatCompletion (attribute-read parity)."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _usage(
    *, prompt: int = 100, completion: int = 20, cached: int | None = None
) -> SimpleNamespace:
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.next_completion: Any = None
        self.raise_exc: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.next_completion


class _FakeModels:
    def __init__(self) -> None:
        self.raise_exc: Exception | None = None
        self.called = False

    async def list(self) -> Any:
        self.called = True
        if self.raise_exc is not None:
            raise self.raise_exc
        return SimpleNamespace(data=[])


class _FakeAsyncOpenAI:
    """Stand-in for openai.AsyncOpenAI: exposes chat.completions and models, plus with_options."""

    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.models = _FakeModels()
        self.options_key: str | None = None

    def with_options(self, *, api_key: str) -> _FakeAsyncOpenAI:
        self.options_key = api_key
        return self


def _client_with_fake() -> tuple[OpenAIClient, _FakeAsyncOpenAI]:
    client = OpenAIClient()
    fake = _FakeAsyncOpenAI()
    client._client = fake  # type: ignore[assignment]
    return client, fake


def _req() -> httpx.Request:
    return httpx.Request("GET", "https://api.openai.com/v1/x")


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "unauthorized", response=httpx.Response(401, request=_req()), body=None
    )


# ============================ finish_reason → canonical stop_reason ============================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("tool_calls", STOP_REASON_TOOL_USE),
        ("length", STOP_REASON_MAX_TOKENS),
        ("stop", STOP_REASON_END_TURN),
        ("content_filter", STOP_REASON_END_TURN),
        ("function_call", STOP_REASON_END_TURN),  # unknown/other → end_turn
        (None, STOP_REASON_END_TURN),
    ],
)
async def test_finish_reason_maps_to_canonical_stop_reason(
    finish_reason: str | None, expected: str
) -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(finish_reason=finish_reason, usage=_usage())
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == expected


# ============================ tool_calls → domain tool_uses ============================
@pytest.mark.asyncio
async def test_tool_calls_reverse_mapped_and_arguments_parsed() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[
            _function_tool_call("call_a", "files_read", '{"path": "a.txt"}'),
            _function_tool_call("call_b", "calendar_read", '{"start": "x", "end": "y"}'),
        ],
        usage=_usage(),
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.stop_reason == STOP_REASON_TOOL_USE
    # underscore wire name → dotted domain name; arguments JSON-string → dict; raw call id kept.
    assert result.tool_uses == [
        {"id": "call_a", "name": "files.read", "input": {"path": "a.txt"}},
        {"id": "call_b", "name": "calendar.read", "input": {"start": "x", "end": "y"}},
    ]
    # content_blocks: the normalized OpenAI assistant message (single dict) for persist/replay.
    assert len(result.content_blocks) == 1
    msg = result.content_blocks[0]
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0] == {
        "id": "call_a",
        "type": "function",
        "function": {"name": "files_read", "arguments": '{"path": "a.txt"}'},
    }


@pytest.mark.asyncio
async def test_tool_call_invalid_json_arguments_raises_validation_failed() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[_function_tool_call("call_x", "files_read", "{not json")],
        usage=_usage(),
    )
    with pytest.raises(ValidationFailedError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_tool_call_non_object_arguments_raises_validation_failed() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[_function_tool_call("call_x", "files_read", "[1, 2, 3]")],
        usage=_usage(),
    )
    with pytest.raises(ValidationFailedError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_tool_call_unknown_name_raises_validation_failed() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[_function_tool_call("call_x", "totally_unknown_tool", "{}")],
        usage=_usage(),
    )
    with pytest.raises(ValidationFailedError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_tool_call_empty_arguments_string_becomes_empty_dict() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[_function_tool_call("call_x", "files_list", "")],
        usage=_usage(),
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.tool_uses == [{"id": "call_x", "name": "files.list", "input": {}}]


# ============================ usage parsing ============================
@pytest.mark.asyncio
async def test_usage_cached_tokens_map_to_cache_read_write_zero() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(
        usage=_usage(prompt=300, completion=40, cached=128)
    )
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 40
    assert result.usage.cache_read_tokens == 128
    assert result.usage.cache_write_tokens == 0  # OpenAI has no explicit write count
    assert result.usage.model == get_settings().openai_model


@pytest.mark.asyncio
async def test_usage_without_details_defaults_cache_read_zero() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage(cached=None))
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_write_tokens == 0


@pytest.mark.asyncio
async def test_usage_absent_yields_zeros() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=None)
    result = await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.cache_read_tokens == 0


# ============================ message building from neutral history ============================
@pytest.mark.asyncio
async def test_build_messages_includes_system_and_tool_role_with_call_id() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    messages = [
        NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "hi"}]),
        NeutralMessage(
            role="assistant",
            content_blocks=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "files_read", "arguments": "{}"},
                        }
                    ],
                }
            ],
        ),
        NeutralMessage(
            role="tool",
            tool_call_id="dom-1",
            provider_tool_use_id="call_1",
            tool_name="files.read",
            result={"ok": True},
        ),
    ]
    await client.create_message(
        system_prompt="SYSTEM", messages=messages, tools=[], attachments=None
    )
    sent = fake.completions.calls[0]["messages"]
    assert sent[0] == {"role": "system", "content": "SYSTEM"}
    # user message: text concatenated into a string.
    assert sent[1] == {"role": "user", "content": "hi"}
    # assistant message replayed verbatim (already OpenAI-shaped).
    assert sent[2]["role"] == "assistant"
    assert sent[2]["tool_calls"][0]["id"] == "call_1"
    # tool message carries tool_call_id = raw provider id and JSON-encoded result content.
    assert sent[3]["role"] == "tool"
    assert sent[3]["tool_call_id"] == "call_1"
    assert json.loads(sent[3]["content"]) == {"ok": True}


@pytest.mark.asyncio
async def test_tool_role_error_serialized_into_content() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    messages = [
        NeutralMessage(
            role="tool",
            provider_tool_use_id="call_1",
            error={"message": "boom", "code": "x"},
        ),
    ]
    await client.create_message(system_prompt="s", messages=messages, tools=[], attachments=None)
    tool_msg = fake.completions.calls[0]["messages"][1]
    assert tool_msg["role"] == "tool"
    assert json.loads(tool_msg["content"]) == {"message": "boom", "code": "x"}


# ============================ tool definitions (OpenAI wire) + gating ============================
@pytest.mark.asyncio
async def test_serialize_tools_to_openai_function_format() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=True),
        attachments=None,
    )
    sent_tools = fake.completions.calls[0]["tools"]
    # every tool is {type:function, function:{name(underscore), description, parameters}}.
    for t in sent_tools:
        assert t["type"] == "function"
        fn = t["function"]
        assert set(fn) == {"name", "description", "parameters"}
        assert "." not in fn["name"]  # underscore transport name, dots forbidden
        assert "_" in fn["name"] or fn["name"].isalnum()
    names = {t["function"]["name"] for t in sent_tools}
    assert "files_read" in names
    assert "site_write_file" in names  # server-side offered when include_server_side=True
    assert "time_now" in names


@pytest.mark.asyncio
async def test_server_side_gating_excludes_site_tools_when_no_project() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    await client.create_message(
        system_prompt="s",
        messages=[],
        tools=neutral_tool_definitions(include_server_side=False),
        attachments=None,
    )
    names = {t["function"]["name"] for t in fake.completions.calls[0]["tools"]}
    assert not any(n.startswith("site_") for n in names)  # site.* excluded
    assert "files_read" in names  # client-side still offered
    assert "time_now" in names  # global server-side always offered (ADR-026)


@pytest.mark.asyncio
async def test_no_tools_passes_not_given() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert fake.completions.calls[0]["tools"] is openai.NOT_GIVEN


# ============================ attachments (OpenAI mapping) ============================
def _png_b64() -> str:
    import base64

    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode("ascii")


def test_openai_attachment_image_maps_to_image_url_data_uri() -> None:
    prepared = prepare_attachments(
        [AttachmentIn(type="image", mediaType="image/png", filename="p.png", data=_png_b64())],
        get_settings(),
        provider="openai",
    )
    block = prepared.content_blocks[0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_attachment_text_maps_to_text_block() -> None:
    import base64

    data = base64.b64encode(b"hello world").decode("ascii")
    prepared = prepare_attachments(
        [AttachmentIn(type="text", mediaType="text/plain", filename="n.txt", data=data)],
        get_settings(),
        provider="openai",
    )
    block = prepared.content_blocks[0]
    assert block["type"] == "text"
    assert "hello world" in block["text"]


def _pdf_b64(pages: int = 1, *, encrypt: str | None = None) -> str:
    import base64
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypt is not None:
        writer.encrypt(encrypt)
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_openai_attachment_pdf_maps_to_native_file_input() -> None:
    # ADR-041 (closes TD-023): PDF on OpenAI is no longer 422 — it becomes a native Chat
    # Completions file-input content-part with a data-URI file_data (NOT a rejection).
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="doc.pdf", data=pdf_b64
    )
    prepared = prepare_attachments([att], get_settings(), provider="openai")

    assert len(prepared.content_blocks) == 1
    block = prepared.content_blocks[0]
    assert block["type"] == "file"
    file_part = block["file"]
    assert isinstance(file_part, dict)
    assert file_part["filename"] == "doc.pdf"
    assert file_part["file_data"] == f"data:application/pdf;base64,{pdf_b64}"
    # The raw base64 the client sent is carried verbatim in the in-memory data-URI.
    assert file_part["file_data"].startswith("data:application/pdf;base64,")


def test_openai_attachment_pdf_default_filename_when_none() -> None:
    # ADR-041 §3: filename=None -> deterministic default "file" in the file-part.
    att = AttachmentIn(type="document", mediaType="application/pdf", filename=None, data=_pdf_b64())
    block = prepare_attachments([att], get_settings(), provider="openai").content_blocks[0]
    assert block["file"]["filename"] == "file"


def test_openai_attachment_pdf_custom_filename_passthrough() -> None:
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="report-Q3.pdf", data=_pdf_b64()
    )
    block = prepare_attachments([att], get_settings(), provider="openai").content_blocks[0]
    assert block["file"]["filename"] == "report-Q3.pdf"


def test_openai_attachment_pdf_storage_invariant_placeholder_no_base64() -> None:
    # ADR-041 §5 / ADR-020 §3: raw base64 / file_data lives ONLY in the in-memory content block,
    # never in the persisted placeholder (chat_steps.payload).
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="secret.pdf", data=pdf_b64
    )
    prepared = prepare_attachments([att], get_settings(), provider="openai")

    assert len(prepared.placeholders) == 1
    ph = prepared.placeholders[0]
    assert ph["type"] == "text"
    assert pdf_b64 not in ph["text"]  # no raw base64
    assert "file_data" not in ph["text"]  # no data-URI key leaked
    assert "data:application/pdf;base64," not in ph["text"]
    assert "application/pdf" in ph["text"]  # human-readable metadata only
    assert "secret.pdf" in ph["text"]


def test_openai_attachment_pdf_file_data_only_in_content_block_not_placeholder() -> None:
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(type="document", mediaType="application/pdf", data=pdf_b64)
    prepared = prepare_attachments([att], get_settings(), provider="openai")
    # file_data present in-memory...
    assert prepared.content_blocks[0]["file"]["file_data"].endswith(pdf_b64)
    # ...and absent from the persisted side.
    assert pdf_b64 not in prepared.placeholders[0]["text"]


# --- ADR-041 §4: shared validation still applies BEFORE the openai/document branch (still 422) ---
def test_openai_attachment_encrypted_pdf_still_422() -> None:
    att = AttachmentIn(
        type="document", mediaType="application/pdf", data=_pdf_b64(encrypt="secret")
    )
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], get_settings(), provider="openai")


def test_openai_attachment_corrupt_pdf_still_422() -> None:
    import base64

    bad = base64.b64encode(b"%PDF-1.4\nnot a real pdf body\n%%EOF").decode("ascii")
    att = AttachmentIn(type="document", mediaType="application/pdf", data=bad)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], get_settings(), provider="openai")


def test_openai_attachment_pdf_magic_byte_spoof_still_422() -> None:
    import base64

    bad = base64.b64encode(b"%NOTPDF" + b"\x00" * 32).decode("ascii")
    att = AttachmentIn(type="document", mediaType="application/pdf", data=bad)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], get_settings(), provider="openai")


def test_openai_attachment_pdf_over_page_limit_still_422() -> None:
    from app.config import Settings

    small = Settings(ATTACHMENT_PDF_MAX_PAGES=2)
    att = AttachmentIn(type="document", mediaType="application/pdf", data=_pdf_b64(3))
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], small, provider="openai")


def test_openai_attachment_pdf_over_size_limit_still_422() -> None:
    import base64

    from app.config import Settings

    small = Settings(ATTACHMENT_MAX_BYTES_DOCUMENT=1024)
    # ~4KB decoded estimate > 1KB document limit -> rejected BEFORE decode (ADR-041 §4).
    big_b64 = base64.b64encode(b"\x00" * 4096).decode("ascii")
    att = AttachmentIn(type="document", mediaType="application/pdf", data=big_b64)
    with pytest.raises(ValidationFailedError):
        prepare_attachments([att], small, provider="openai")


# --- ADR-041 §5: Anthropic PDF mapping is UNCHANGED (native document block), regression ---
def test_anthropic_attachment_pdf_unchanged_native_document_block() -> None:
    # ADR-041 touches ONLY the openai branch; anthropic PDF stays the native document dict.
    pdf_b64 = _pdf_b64()
    att = AttachmentIn(
        type="document", mediaType="application/pdf", filename="doc.pdf", data=pdf_b64
    )
    prepared = prepare_attachments([att], get_settings(), provider="anthropic")
    block = prepared.content_blocks[0]
    assert block == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
    }
    # Symmetric provider-agnostic check: same PDF, openai -> file-input, anthropic -> document.
    openai_block = prepare_attachments([att], get_settings(), provider="openai").content_blocks[0]
    assert openai_block["type"] == "file"
    assert block["type"] == "document"


@pytest.mark.asyncio
async def test_pdf_file_part_injected_into_last_user_message() -> None:
    # ADR-041 §1: the native file-input part is injected into the last user message like image/text.
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    prepared = PreparedAttachments(
        content_blocks=[
            {
                "type": "file",
                "file": {
                    "filename": "doc.pdf",
                    "file_data": "data:application/pdf;base64,JVBERi0=",
                },
            }
        ],
        placeholders=[],
    )
    messages = [NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "read"}])]
    await client.create_message(
        system_prompt="s", messages=messages, tools=[], attachments=prepared
    )
    user_msg = fake.completions.calls[0]["messages"][-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert any(p.get("type") == "file" for p in user_msg["content"])


@pytest.mark.asyncio
async def test_attachments_injected_into_last_user_message_as_parts() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    prepared = PreparedAttachments(
        content_blocks=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        placeholders=[],
    )
    messages = [NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "look"}])]
    await client.create_message(
        system_prompt="s", messages=messages, tools=[], attachments=prepared
    )
    user_msg = fake.completions.calls[0]["messages"][-1]
    assert user_msg["role"] == "user"
    # content becomes a parts list: the text part followed by the image_url part.
    assert isinstance(user_msg["content"], list)
    assert {"type": "text", "text": "look"} in user_msg["content"]
    assert any(p.get("type") == "image_url" for p in user_msg["content"])


# ============================ validate_key ============================
@pytest.mark.asyncio
async def test_validate_key_ok_returns_valid() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = None
    assert await client.validate_key("sk-openai-good") is KeyValidation.valid
    assert fake.options_key == "sk-openai-good"
    assert fake.models.called


@pytest.mark.asyncio
async def test_validate_key_401_returns_invalid() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = _auth_error()
    assert await client.validate_key("sk-bad") is KeyValidation.invalid


@pytest.mark.asyncio
async def test_validate_key_timeout_returns_offline() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = openai.APITimeoutError(request=_req())
    assert await client.validate_key("sk-x") is KeyValidation.offline


@pytest.mark.asyncio
async def test_validate_key_connection_error_returns_offline() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = openai.APIConnectionError(message="conn", request=_req())
    assert await client.validate_key("sk-x") is KeyValidation.offline


@pytest.mark.asyncio
async def test_validate_key_non_401_status_returns_offline() -> None:
    client, fake = _client_with_fake()
    fake.models.raise_exc = openai.APIStatusError(
        "boom", response=httpx.Response(500, request=_req()), body=None
    )
    assert await client.validate_key("sk-x") is KeyValidation.offline


# ============================ upstream error mapping in create_message ============================
@pytest.mark.asyncio
async def test_create_message_auth_error_raises_openai_auth_error() -> None:
    client, fake = _client_with_fake()
    fake.completions.raise_exc = _auth_error()
    with pytest.raises(OpenAIAuthError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_create_message_timeout_raises_upstream_error() -> None:
    client, fake = _client_with_fake()
    fake.completions.raise_exc = openai.APITimeoutError(request=_req())
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_create_message_status_error_raises_upstream_error() -> None:
    client, fake = _client_with_fake()
    fake.completions.raise_exc = openai.APIStatusError(
        "boom", response=httpx.Response(500, request=_req()), body=None
    )
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)


@pytest.mark.asyncio
async def test_byok_api_key_override_applied() -> None:
    client, fake = _client_with_fake()
    fake.completions.next_completion = _completion(usage=_usage())
    await client.create_message(
        system_prompt="s", messages=[], tools=[], attachments=None, api_key="sk-byok"
    )
    assert fake.options_key == "sk-byok"


# ============================ factory get_llm_client() ============================
def test_factory_default_returns_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.chat.anthropic_client as anthropic_mod
    import app.chat.llm_client as llm_mod
    from app.chat.anthropic_client import AnthropicClient

    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", None)
    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    get_settings.cache_clear()
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    try:
        client = get_llm_client()
        assert isinstance(client, AnthropicClient)
    finally:
        get_settings.cache_clear()


def test_factory_anthropic_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.chat.anthropic_client as anthropic_mod
    import app.chat.llm_client as llm_mod
    from app.chat.anthropic_client import AnthropicClient

    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", None)
    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    try:
        assert isinstance(get_llm_client(), AnthropicClient)
    finally:
        get_settings.cache_clear()


def test_factory_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.chat.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    try:
        client = get_llm_client()
        assert isinstance(client, OpenAIClient)
        # singleton: a second call returns the same instance.
        assert get_llm_client() is client
    finally:
        get_settings.cache_clear()
        monkeypatch.setattr(llm_mod, "_openai_singleton", None, raising=False)
