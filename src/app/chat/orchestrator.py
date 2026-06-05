"""Chat Orchestrator (CO-4..CO-7): policy → generate → tool-loop → debit → audit.

Implements /chat/run and /chat/tool-result. Single source of access truth is Policy Engine
(AC-6). messageStepId is the billing idempotency key, one per user message-step, reused
across all tool-rounds and re-entry (ADR-005/006). Debit happens exactly once on the final
assistant_message (mode=credits). BYOK plaintext key is in-memory only, never logged.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_CHAT_STEP,
    EVENT_POLICY_DECISION,
    EVENT_TOOL_CALL_COMPLETED,
    EVENT_TOOL_CALL_INITIATED,
    EVENT_TOOL_MUTATION,
    AuditEvent,
    AuditService,
)
from app.byok.service import BYOKService
from app.chat.anthropic_client import AnthropicAuthError, AnthropicClient, AnthropicResult
from app.chat.attachments import prepare_attachments
from app.chat.repository import ChatRepository, derive_title
from app.chat.tools import (
    MUTATING_TOOLS,
    SERVER_SIDE_TOOLS,
    anthropic_tool_definitions,
    validate_tool_args,
)
from app.config import get_settings
from app.errors import (
    InsufficientCreditsError,
    NotFoundError,
    UpstreamError,
    ValidationFailedError,
)
from app.models import ChatSession, ChatStep
from app.observability.logging import log_event
from app.observability.metrics import (
    blocked_requests_total,
    byok_usage_share,
    token_usage_total,
)
from app.policy.engine import (
    BlockReason,
    Decision,
    Mode,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)
from app.policy.loader import load_policy_state
from app.preferences.service import PreferencesService
from app.schemas.chat import AttachmentIn
from app.wallet.service import WalletService
from app.website.tools import SiteToolHandlers

logger = logging.getLogger("app.chat.orchestrator")

# ADR-012: base system prompt selected by assistant_mode (chat vs code). Single source of truth
# for each mode's prompt (no scattered hardcoding). The set of tools offered to Claude is
# unchanged in this sprint (Q-012-1 default deferred); only the system prompt varies.
_SYSTEM_PROMPT_CHAT = (
    "You are a helpful assistant integrated into an iOS app. You can call tools that the "
    "user's device executes locally (files, calendar, reminders). Use tools when needed and "
    "respond concisely."
)
_SYSTEM_PROMPT_CODE = (
    "You are a coding assistant integrated into an iOS app. Favor precise, technical answers: "
    "produce correct, idiomatic code with brief explanations. You can call tools that the "
    "user's device executes locally (files, calendar, reminders) and server-side site tools. "
    "Use tools when needed and respond concisely."
)


def _system_prompt_for(assistant_mode: str) -> str:
    return _SYSTEM_PROMPT_CODE if assistant_mode == "code" else _SYSTEM_PROMPT_CHAT


@dataclass(frozen=True)
class ToolCallOut:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ChatRunOut:
    status: str  # assistant_message | tool_call | blocked
    session_id: uuid.UUID
    assistant_message: str | None = None
    tool_call: ToolCallOut | None = None
    block_reason: str | None = None
    usage: dict[str, Any] | None = None
    # ADR-023: sync ids for chat history. message_step_id = the turn (one per user message-step,
    # reused across tool-rounds/re-entry); step_id = the id of the persisted assistant/tool step
    # this response represents (= ChatStep.id = ChatStepSchema.id). Both None for blocked (no step
    # / turn is created — policy blocks before generation).
    message_step_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None


@dataclass(frozen=True)
class _TurnOutcome:
    """Result of processing one tool_use turn (ADR-011).

    client_out is set when the turn yields a client-side tool_call to hand off to iOS; None when
    the turn was purely server-side (site.*) and the orchestrator should continue the loop.
    """

    client_out: ChatRunOut | None


@dataclass(frozen=True)
class _BillingPlan:
    """How the final assistant_message must be billed (ADR-002 + ADR-005).

    Exactly one of the two flags is true when billing applies:
    - debit_credits: active subscription + mode=credits → consume 1 credit (idempotent).
    - mark_trial:    subscription=none + trial_used=false + mode=credits → free trial, flip
      users.trial_used (idempotent). No debit.
    BYOK and trial generations are free → both flags false.
    """

    debit_credits: bool
    mark_trial: bool


def _billing_plan(mode: Mode, state: PolicyState) -> _BillingPlan:
    if mode is Mode.byok:
        return _BillingPlan(debit_credits=False, mark_trial=False)
    # mode == credits
    if state.subscription_status is SubscriptionStatus.active:
        # ADR-002: "active + credits>0 → allow + debit". Only here do we charge a credit.
        return _BillingPlan(debit_credits=True, mark_trial=False)
    if state.subscription_status is SubscriptionStatus.none and not state.trial_used:
        # ADR-002: trial-allow has NO debit; instead the lifetime trial is consumed.
        return _BillingPlan(debit_credits=False, mark_trial=True)
    # Any other credits state would have been blocked by policy before reaching here.
    return _BillingPlan(debit_credits=False, mark_trial=False)


@dataclass
class _Deps:
    repo: ChatRepository
    wallet: WalletService
    byok: BYOKService
    audit: AuditService
    anthropic: AnthropicClient
    site_tools: SiteToolHandlers
    preferences: PreferencesService


class ChatOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        repo: ChatRepository,
        wallet: WalletService,
        byok: BYOKService,
        audit: AuditService,
        anthropic_client: AnthropicClient,
        site_tools: SiteToolHandlers,
        preferences: PreferencesService,
    ) -> None:
        self._session = session
        self._deps = _Deps(
            repo=repo,
            wallet=wallet,
            byok=byok,
            audit=audit,
            anthropic=anthropic_client,
            site_tools=site_tools,
            preferences=preferences,
        )

    # ---- public entrypoints ----

    async def run(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        session_id: uuid.UUID | None,
        message: str,
        mode: str,
        assistant_mode: str | None = None,
        attachments: list[AttachmentIn] | None = None,
    ) -> ChatRunOut:
        message_step_id = uuid.uuid4()  # CO-4b: billing key for this user message-step
        # ADR-012: resolve assistant_mode for a NEW session — explicit request → preferences
        # default → 'chat'. Fixed on the session at creation; ignored when resuming a session
        # (assistant_mode is a session attribute). billing_mode (`mode`) is independent.
        resolved_assistant_mode = (
            assistant_mode
            if assistant_mode is not None
            else await self._deps.preferences.get_default_assistant_mode(user_id)
        )
        ctx = await self._deps.repo.get_or_create_session(
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            session_id=session_id,
            assistant_mode=resolved_assistant_mode,
            # Auto-title from the first user message (chats/03); only used for a new session.
            title=derive_title(message),
        )
        sess = ctx.session
        # mode is fixed on the session; use the session's stored mode.
        effective_mode = Mode(sess.mode)
        system_prompt = _system_prompt_for(sess.assistant_mode)

        # ADR-020: validate inline attachments and split into (a) full content blocks sent to
        # Claude ONCE on turn 0 (in-memory only) and (b) light text placeholders persisted in
        # chat_steps.payload. Raw base64 is NEVER persisted (storage invariant). Validation runs
        # BEFORE persisting the user step so a bad attachment is a clean 422 with no DB write.
        first_turn_content: list[dict[str, Any]] | None = None
        user_payload_content: list[dict[str, Any]] = [{"type": "text", "text": message}]
        if attachments:
            prepared = prepare_attachments(attachments, get_settings())
            first_turn_content = [
                {"type": "text", "text": message},
                *prepared.content_blocks,
            ]
            user_payload_content = [
                {"type": "text", "text": message},
                *prepared.placeholders,
            ]

        # Persist the user message under this step (placeholders only — no base64, ADR-020 §3).
        await self._deps.repo.add_step(
            session_id=sess.id,
            message_step_id=message_step_id,
            role="user",
            payload={"content": user_payload_content},
        )

        decision, state = await self._evaluate(user_id, effective_mode, sess.id)
        if not decision_allow(decision):
            return self._blocked(sess.id, decision.block_reason)

        # mode=byok: resolve plaintext key in-memory (CO-6).
        api_key = await self._resolve_api_key(user_id, effective_mode)

        return await self._generate_loop(
            user_id=user_id,
            session_id=sess.id,
            message_step_id=message_step_id,
            mode=effective_mode,
            billing=_billing_plan(effective_mode, state),
            api_key=api_key,
            system_prompt=system_prompt,
            # ADR-022 axis A: offer site.* only when the session has a project.
            has_project=sess.project_id is not None,
            first_turn_user_content=first_turn_content,
        )

    async def tool_result(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> ChatRunOut:
        tool_call = await self._deps.repo.get_tool_call(tool_call_id)
        if tool_call is None or tool_call.session_id != session_id:
            raise NotFoundError("tool call not found for session")
        # Ownership: the session must belong to the user.
        sess = await self._deps.repo.get_session(session_id, user_id)
        if sess is None:
            raise NotFoundError("session not found")

        message_step_id = tool_call.message_step_id  # re-entry: reuse the billing key

        # Idempotent replay: already completed → return the saved next step.
        if tool_call.status == "completed":
            saved = await self._deps.repo.next_step_after(session_id, message_step_id, tool_call_id)
            return self._render_saved_step(session_id, message_step_id, saved)

        # Atomic pending → completed/errored.
        status = "errored" if error is not None else "completed"
        transitioned = await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id, status=status, result=result if result is not None else error
        )
        if not transitioned:
            # Concurrent completion won the race → behave idempotently.
            saved = await self._deps.repo.next_step_after(session_id, message_step_id, tool_call_id)
            return self._render_saved_step(session_id, message_step_id, saved)

        # Persist the tool_result as a tool step. (result size limit is enforced at the
        # schema layer; result content is opaque per-tool and forwarded to Claude as-is.)
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                # ADR-008: tool_result.tool_use_id MUST equal the raw provider id of the matching
                # tool_use block, NOT the domain UUID. Stored here so _build_messages replays the
                # continuation history with a consistent id pair.
                "providerToolUseId": tool_call.provider_tool_use_id,
                "toolName": tool_call.tool_name,
                "result": result,
                "error": error,
            },
        )

        # Audit mutating tool completion (AC-7).
        if tool_call.tool_name in MUTATING_TOOLS:
            await self._deps.audit.record(
                AuditEvent(
                    user_id=user_id,
                    session_id=session_id,
                    event_type=EVENT_TOOL_MUTATION,
                    payload={
                        "toolCallId": str(tool_call_id),
                        "toolName": tool_call.tool_name,
                        "status": status,
                    },
                )
            )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call_id),
                    "toolName": tool_call.tool_name,
                    "status": status,
                },
            )
        )

        mode = Mode(sess.mode)
        # Re-evaluate policy (access may have changed).
        decision, state = await self._evaluate(user_id, mode, session_id)
        if not decision_allow(decision):
            return self._blocked(session_id, decision.block_reason)

        api_key = await self._resolve_api_key(user_id, mode)
        return await self._generate_loop(
            user_id=user_id,
            session_id=session_id,
            message_step_id=message_step_id,
            mode=mode,
            billing=_billing_plan(mode, state),
            api_key=api_key,
            system_prompt=_system_prompt_for(sess.assistant_mode),
            # ADR-022 axis A: project_id is session-fixed; gate site.* by the session's project.
            has_project=sess.project_id is not None,
        )

    # ---- internals ----

    async def _evaluate(
        self, user_id: uuid.UUID, mode: Mode, session_id: uuid.UUID
    ) -> tuple[Decision, PolicyState]:
        state = await load_policy_state(self._session, user_id)
        decision = evaluate(state, mode)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_POLICY_DECISION,
                payload={
                    "mode": mode.value,
                    "decision": "allow" if decision.allow else "blocked",
                    "blockReason": decision.block_reason.value if decision.block_reason else None,
                },
            )
        )
        log_event(
            logger,
            logging.INFO,
            "policy_decision",
            mode=mode.value,
            allow=decision.allow,
            blockReason=decision.block_reason.value if decision.block_reason else None,
        )
        return decision, state

    def _blocked(self, session_id: uuid.UUID, reason: BlockReason | None) -> ChatRunOut:
        resolved = reason or BlockReason.policy_denied
        blocked_requests_total.labels(reason=resolved.value).inc()
        return ChatRunOut(status="blocked", session_id=session_id, block_reason=resolved.value)

    async def _resolve_api_key(self, user_id: uuid.UUID, mode: Mode) -> str | None:
        if mode is Mode.byok:
            byok_usage_share.set(1)
            key = await self._deps.byok.get_plaintext_key(user_id)
            if key is None:
                # Policy should have blocked this; defensive.
                raise ValidationFailedError("byok key unavailable")
            return key
        byok_usage_share.set(0)
        return None  # service key used by AnthropicClient

    async def _build_messages(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Reconstruct Anthropic messages from chat_steps (TD-002)."""
        steps = await self._deps.repo.list_steps(session_id)
        messages: list[dict[str, Any]] = []
        for step in steps:
            payload = step.payload
            if step.role == "user":
                messages.append({"role": "user", "content": payload["content"]})
            elif step.role == "assistant":
                messages.append({"role": "assistant", "content": payload["content"]})
            elif step.role == "tool":
                # ADR-008 / BUG-4: tool_result.tool_use_id MUST be the raw provider id
                # (toolu_...) of the matching tool_use block, NEVER the domain toolCallId (UUID)
                # nor a fresh uuid4. Anthropic rejects a mismatch with 400 → backend 502.
                tool_use_id = payload["providerToolUseId"]
                if payload.get("error") is not None:
                    content = str(payload["error"].get("message", "tool error"))
                    is_error = True
                else:
                    import json

                    content = json.dumps(payload.get("result"))
                    is_error = False
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": content,
                                "is_error": is_error,
                            }
                        ],
                    }
                )
        return messages

    async def _generate_loop(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        mode: Mode,
        billing: _BillingPlan,
        api_key: str | None,
        system_prompt: str,
        has_project: bool,
        first_turn_user_content: list[dict[str, Any]] | None = None,
    ) -> ChatRunOut:
        # ADR-011: server-side site.* tools are executed by the backend synchronously inside this
        # loop, WITHOUT a round-trip to iOS. We keep calling Anthropic as long as the turn contains
        # ONLY server-side tools (their tool_results are produced here and fed straight back).
        # A turn with any client-side tool returns status=tool_call to iOS as before. A pure
        # assistant turn is the final step. The loop is bounded by MAX_SERVER_TOOL_ROUNDS (§2).
        max_rounds = get_settings().max_server_tool_rounds
        # ADR-020: the FULL attachment content blocks are injected into the last user turn on the
        # first iteration ONLY; subsequent (tool-loop) iterations replay placeholders from
        # chat_steps. The override is consumed after the first iteration so heavy base64 is never
        # re-sent to Anthropic.
        turn0_override = first_turn_user_content
        for _ in range(max_rounds + 1):
            messages = await self._build_messages(session_id)
            if turn0_override is not None:
                # Replace the last user turn's content (placeholders) with the full blocks for the
                # single first call; then drop the override so later rounds use placeholders only.
                for msg in reversed(messages):
                    if msg["role"] == "user":
                        msg["content"] = turn0_override
                        break
                turn0_override = None
            # MAJOR-4: commit the persisted steps + audit BEFORE the network call so the pooled DB
            # connection is not held open for the whole Anthropic generation. Each subsequent
            # server-side round commits its own persisted tool_use/tool_result before re-calling.
            await self._session.commit()
            try:
                result: AnthropicResult = await self._deps.anthropic.create_message(
                    system_prompt=system_prompt,
                    messages=messages,
                    # ADR-022 axis A: in «чистый чат» (no project) site.* (SERVER_SIDE_TOOLS) are
                    # NOT offered to Claude. Axis B (assistant_mode, Q-012-1) is not yet
                    # implemented; the effective set = this project gate over current behavior.
                    tools=anthropic_tool_definitions(include_server_side=has_project),
                    api_key=api_key,
                )
            except AnthropicAuthError:
                if mode is Mode.byok:
                    # ADR-016: a previously-valid BYOK key rejected with 401 on use → expired
                    # (revoked/expired), not freshly invalid. Both map to byok_invalid in policy.
                    await self._deps.byok.mark_expired(user_id)
                    await self._session.commit()
                    return self._blocked(session_id, BlockReason.byok_invalid)
                raise

            usage = result.usage.to_dict()
            token_usage_total.labels(direction="input", model=result.usage.model).inc(
                result.usage.input_tokens
            )
            token_usage_total.labels(direction="output", model=result.usage.model).inc(
                result.usage.output_tokens
            )

            if result.stop_reason == "tool_use" and result.tool_uses:
                outcome = await self._handle_tool_use(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    result=result,
                    usage=usage,
                    has_project=has_project,
                )
                # Persist the tool_use step + tool_calls + tool_results + audit (no billing here).
                await self._session.commit()
                if outcome.client_out is not None:
                    # A client-side tool is pending → hand off to iOS (drop the plaintext key).
                    api_key = None
                    return outcome.client_out
                # Pure server-side turn: results are persisted; continue the loop to Anthropic.
                continue

            # Final assistant_message — break out of the server-side loop and bill once.
            api_key = None
            return await self._finalize_assistant(
                user_id=user_id,
                session_id=session_id,
                message_step_id=message_step_id,
                billing=billing,
                result=result,
                usage=usage,
            )

        # Exceeded MAX_SERVER_TOOL_ROUNDS consecutive server-side rounds (ADR-011 §2): controlled
        # failure + audit, never an infinite loop. No billing (no final assistant_message).
        api_key = None
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "error": "max_server_tool_rounds_exceeded",
                    "maxRounds": max_rounds,
                },
            )
        )
        await self._session.commit()
        raise UpstreamError("server-side tool loop exceeded maximum rounds")

    async def _external_project_id(self, session_id: uuid.UUID) -> str:
        """external_project_id for site.* tools — from chat_sessions.project_id (session context).

        Never from model-supplied tool args (IDOR guard, website-builder/05-security.md).
        ADR-022 defensive-guard: called ONLY for sessions with a project (`project_id IS NOT NULL`);
        a NULL here is an upstream anomaly (site.* should not have been offered/executed).
        """
        sess = await self._session.get(ChatSession, session_id)
        if sess is None:  # pragma: no cover - session was just created/validated upstream
            raise NotFoundError("session not found")
        if sess.project_id is None:  # pragma: no cover - guarded by has_project before this call
            raise UpstreamError("site.* resolution attempted for a project-less session")
        return sess.project_id

    async def _finalize_assistant(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        billing: _BillingPlan,
        result: AnthropicResult,
        usage: dict[str, Any],
    ) -> ChatRunOut:
        # Final assistant_message. The assistant-step + billing (debit or trial flip) + audit are
        # committed together as one short transaction (atomicity per MAJOR-4 / CRITICAL-1).
        # ADR-023: capture the persisted assistant step's id → ChatResponse.stepId. It is the same
        # ChatStep.id that GET /v1/chats/{id} renders as ChatStepSchema.id for this step (sync
        # invariant).
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )
        sess = await self._session.get(ChatSession, session_id)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "role": "assistant",
                    "model": usage.get("model"),
                    "usage": usage,
                },
            )
        )

        # CO-7 / ADR-002 / ADR-005: bill exactly once on the final assistant_message.
        # - active subscription + credits → consume 1 credit;
        # - trial (subscription=none, trial_used=false) → free, flip users.trial_used;
        # - byok / already-trial-used → free, no write.
        if billing.debit_credits:
            try:
                await self._debit(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    usage=usage,
                )
            except InsufficientCreditsError:
                # Balance dropped below 1 after policy allow → business block, not a tech error.
                # Roll back the assistant-step+audit so the unbillable step is not persisted.
                await self._session.rollback()
                return self._blocked(session_id, BlockReason.credits_empty)
        elif billing.mark_trial:
            # CRITICAL-1: consume the single lifetime trial atomically (idempotent).
            await self._deps.repo.mark_trial_used(user_id)

        if sess is not None:
            await self._deps.repo.touch_session(sess)

        await self._session.commit()
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=result.text,
            usage=usage,
            message_step_id=message_step_id,
            step_id=assistant_step.id,
        )

    async def _handle_tool_use(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        result: AnthropicResult,
        usage: dict[str, Any],
        has_project: bool,
    ) -> _TurnOutcome:
        """Process a tool_use turn (ADR-008/011): persist tool_calls, branch server/client-side.

        For every tool_use block a tool_call row is persisted with its own domain id (uuid4) and
        raw provider_tool_use_id (toolu_..., never derived from the anthropic id — BUG-4). Then:
        - server-side (site.*): executed on the backend NOW; tool_call goes straight to status
          completed with the backend result; a tool step records the tool_result (replayed to
          Anthropic on continuation, ADR-011 §4). No round-trip to iOS.
        - client-side (files.*/...): left pending; the FIRST one is returned as status=tool_call
          to iOS (public contract returns a single toolCall, 02-api-contracts.md).
        If the turn contains any client-side tool, client_out is set (hand off to iOS). If the turn
        is purely server-side, client_out is None and the orchestrator continues the loop.
        """
        # Persist the assistant tool_use step (no debit on tool-rounds). ADR-023: this is the
        # step-of-record for a status=tool_call response — ChatResponse.stepId = its ChatStep.id
        # (the history step whose payload carries the tool_use block). NOT toolCall.id.
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )

        # ADR-022 §2/§4 defensive-guard: _external_project_id() (which resolves the project for
        # site.* execution) is resolved ONLY when the session has a project. Without a project,
        # site.* were not offered to Claude, so this path is unreachable in normal operation; if
        # Claude returns a site.* tool_use anyway (upstream anomaly), we must NOT execute it and
        # must NOT resolve a project — see the per-block guard below.
        external_project_id = await self._external_project_id(session_id) if has_project else None
        first_client_out: ToolCallOut | None = None
        for block in result.tool_uses:
            tool_name = str(block["name"])
            provider_tool_use_id = str(block["id"])  # raw anthropic "toolu_...", opaque

            # ADR-022 defensive-guard: a server-side site.* tool_use with no project must never be
            # executed (the tool was not offered; this is an upstream anomaly, treated like an
            # unknown tool name — ADR-008). Fail before validating args / resolving any project.
            if tool_name in SERVER_SIDE_TOOLS and not has_project:
                raise UpstreamError("server-side site.* tool requested for a project-less session")

            try:
                validated_args = validate_tool_args(tool_name, dict(block["input"]))
            except ValueError as exc:
                raise ValidationFailedError(str(exc)) from exc

            tool_call_id = uuid.uuid4()  # domain id: fresh UUID, independent of anthropic id
            await self._deps.repo.create_tool_call(
                session_id=session_id,
                message_step_id=message_step_id,
                tool_name=tool_name,
                args=validated_args,
                tool_call_id=tool_call_id,
                provider_tool_use_id=provider_tool_use_id,
            )
            await self._deps.audit.record(
                AuditEvent(
                    user_id=user_id,
                    session_id=session_id,
                    event_type=EVENT_TOOL_CALL_INITIATED,
                    payload={"toolCallId": str(tool_call_id), "toolName": tool_name},
                )
            )

            if tool_name in SERVER_SIDE_TOOLS:
                # Invariant (ADR-022): reaching here implies has_project is True (the project-less
                # site.* anomaly raised above), so external_project_id is a resolved string.
                assert external_project_id is not None  # noqa: S101 - ADR-022 guard invariant
                await self._execute_server_side_tool(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=validated_args,
                    provider_tool_use_id=provider_tool_use_id,
                    external_project_id=external_project_id,
                )
            elif first_client_out is None:
                first_client_out = ToolCallOut(
                    id=str(tool_call_id), name=tool_name, args=validated_args
                )

        if first_client_out is not None:
            return _TurnOutcome(
                client_out=ChatRunOut(
                    status="tool_call",
                    session_id=session_id,
                    tool_call=first_client_out,
                    usage=usage,
                    message_step_id=message_step_id,
                    step_id=assistant_step.id,
                )
            )
        # Purely server-side turn → continue the loop (no hand-off to iOS).
        return _TurnOutcome(client_out=None)

    async def _execute_server_side_tool(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        provider_tool_use_id: str,
        external_project_id: str,
    ) -> None:
        """Execute a site.* tool on the backend and persist its tool_result (ADR-011 §1, §4).

        The tool_call is moved to status=completed immediately (no client tool_result is awaited).
        The tool step stores the providerToolUseId so _build_messages replays the continuation with
        a consistent id pair (ADR-008). MUTATING audit (site.write_file/site.delete → tool_mutation)
        is recorded inside the handler, in this same transaction (audit/03-architecture).
        """
        execution = await self._deps.site_tools.execute(
            tool_name=tool_name,
            args=args,
            user_id=user_id,
            external_project_id=external_project_id,
            session_id=session_id,
        )
        payload = execution.to_tool_result_payload()
        status = "errored" if execution.is_error else "completed"
        await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            result=payload,
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                "providerToolUseId": provider_tool_use_id,
                "toolName": tool_name,
                "result": payload.get("result"),
                "error": payload.get("error"),
            },
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call_id),
                    "toolName": tool_name,
                    "status": status,
                },
            )
        )

    async def _debit(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        usage: dict[str, Any],
    ) -> None:
        # amount is fixed at 1 (1 credit = 1 message, ADR-006); idempotent by messageStepId.
        # InsufficientCreditsError propagates to the caller, which maps it to a credits_empty block.
        await self._deps.wallet.consume(
            user_id=user_id,
            amount=1,
            idempotency_key=str(message_step_id),
            meta={"usage": usage, "model": usage.get("model")},
            session_id=session_id,
        )

    def _render_saved_step(
        self,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        step: ChatStep | None,
    ) -> ChatRunOut:
        if step is None:
            # Nothing generated yet for this step (e.g. concurrent in-flight) → treat as not found.
            raise NotFoundError("no completed step for tool result")
        text = ""
        for block in step.payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        # ADR-023: idempotent replay returns the same sync ids as the original response — the turn
        # (message_step_id, stable across re-entry) and the saved step's own id.
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=text,
            usage=step.usage,
            message_step_id=message_step_id,
            step_id=step.id,
        )


def decision_allow(decision: Decision) -> bool:
    return decision.allow
