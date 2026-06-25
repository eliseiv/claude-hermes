"""FastAPI dependencies: auth, db session, owner check, service wiring (api-gateway/03)."""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminService
from app.agent_proxy.service import AgentProxyService
from app.api_gateway.auth import AuthenticatedUser, get_jwt_verifier, verify_client_api_key
from app.api_gateway.openapi_security import client_api_key_scheme, user_id_scheme
from app.audit.service import AuditService
from app.auth.apple import get_apple_verifier
from app.auth.issuer import TokenIssuer
from app.auth.service import AuthService
from app.billing_adapty.service import AdaptyWebhookService
from app.byok.kms import get_kms_client
from app.byok.service import BYOKService
from app.chat.global_tools import GlobalToolHandlers, SystemClock
from app.chat.llm_client import get_llm_client
from app.chat.orchestrator import ChatOrchestrator
from app.chat.repository import ChatRepository
from app.chats.repository import ChatsRepository
from app.chats.service import ChatsService
from app.config import get_settings
from app.db import session_scope
from app.errors import UnauthorizedError
from app.hermes_runtime.docker_backend import DockerBackend, RuntimeBackend
from app.hermes_runtime.manager import HermesInstanceManager
from app.hermes_runtime.registry import HermesInstanceRegistry
from app.observability.context import set_user_id
from app.preferences.service import PreferencesService
from app.profile.service import ProfileService
from app.subscription.service import SubscriptionService
from app.subscription.storekit import get_storekit_verifier
from app.token_purchase.service import TokenPurchaseService
from app.wallet.service import WalletService
from app.website.service import WebsiteService
from app.website.tools import SiteToolHandlers
from app.workspaces.repository import WorkspacesRepository
from app.workspaces.service import WorkspacesService


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in session_scope():
        yield session


def verify_bearer_token(authorization: str | None) -> AuthenticatedUser:
    """Verify the Bearer JWT (signature/exp/iss/aud) and extract the trusted subject.

    Pure, side-effect-free (no DB, no logging of the token) so it stays unit-testable in
    isolation. Identity comes exclusively from the verified ``sub`` claim (ADR-007).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return get_jwt_verifier().verify(token)


async def provision_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Lazy, idempotent provisioning of the ``users`` row for a verified subject (ADR-007).

    Runs in the *same* per-request session that downstream use-cases use for their FK-bearing
    inserts (subscriptions/wallets/byok_keys/ledger/chat_sessions). The statement is emitted
    immediately against the connection, so the row is visible to every later statement of this
    transaction *before* any FK insert — and is committed together with them. ``ON CONFLICT
    (id) DO NOTHING`` is atomic in PostgreSQL: concurrent first requests for the same ``sub``
    cannot race or duplicate, and an already-provisioned user's ``trial_used``/``created_at``
    are never overwritten. ``created_at``/``trial_used`` come from the DDL defaults.
    """
    await session.execute(
        text("INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING"),
        {"sub": str(user_id)},
    )


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    api_key: Annotated[str | None, Depends(client_api_key_scheme)] = None,
    x_user_id: Annotated[str | None, Depends(user_id_scheme)] = None,
) -> AuthenticatedUser:
    """Authenticate the client contour and lazily provision the user (ADR-044, ADR-007).

    Single point through which all authenticated ``/v1/*`` requests pass, so the lazy
    provisioning here uniformly covers every endpoint without per-flow duplication.

    Auth model (ADR-044): the trusted client key ``X-API-Key`` authenticates the request
    (``verify_client_api_key``, constant-time, 401 on miss); the subject identity is the
    ``X-User-Id`` header (UUID), trusted because the client key is trusted. Both arrive via
    ``SecurityBase`` schemes (``client_api_key_scheme`` / ``user_id_scheme``, ``APIKeyHeader``,
    ``auto_error=False``): they contribute the ``clientApiKey`` + ``userId`` security schemes to
    OpenAPI (lock icon / Authorize) *without* adding duplicate header parameters to the operation.
    ``auto_error=False`` keeps them from raising on a missing/malformed header — the real 401 stays
    here so behaviour is explicit (08-api-doc R2).

    Provisioning happens only *after* the key is verified and the subject UUID is parsed (an
    invalid key or missing/malformed ``X-User-Id`` raises 401 before any row is created) and
    *before* the subject is used downstream. The JWT/Apple contour stays dormant (ADR-044 §4): the
    source of identity moves from JWT ``sub`` to ``X-User-Id``; provisioning semantics are intact.
    """
    # 1) Authenticate the request by the trusted client key (401 on missing/mismatch).
    verify_client_api_key(api_key)
    # 2) Resolve the trusted subject from X-User-Id (no subject => 401, like a missing identity).
    if not x_user_id:
        raise UnauthorizedError("missing user id")
    try:
        user_id = uuid.UUID(x_user_id.strip())
    except ValueError as exc:
        raise UnauthorizedError("user id is not a valid uuid") from exc
    user = AuthenticatedUser(user_id=user_id, device_id=None)
    set_user_id(str(user.user_id))
    # FastAPI caches `get_db` per request, so `session` is the exact session the service
    # dependencies (orchestrator/wallet/subscription/byok) receive — the upsert lands in the
    # same transaction as their FK-bearing inserts (ADR-044 §2, unchanged from ADR-007).
    await provision_user(session, user.user_id)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def require_owner(body_user_id: uuid.UUID, current: AuthenticatedUser) -> None:
    """No-op on the client contour (ADR-044 §3).

    With a single trusted client key the subject is ``X-User-Id`` by definition, so the old
    "body ``userId`` must equal ``sub``" cross-check has no meaning — there is no independently
    authenticated ``sub`` to disagree with. The symbol is kept (not removed) so the existing
    router call sites (byok/chat/subscription/token_purchase/wallet) keep importing it without
    change; it deliberately performs no check. The dormant JWT contour (ADR-044 §4) does not run
    this path on the hot client path.
    """
    return None


_token_issuer_singleton: TokenIssuer | None = None


def get_token_issuer() -> TokenIssuer:
    """Process-wide TokenIssuer (RS256). Reads the key pair once from the cached settings."""
    global _token_issuer_singleton
    if _token_issuer_singleton is None:
        _token_issuer_singleton = TokenIssuer(get_settings())
    return _token_issuer_singleton


def get_auth_service(session: DbSession) -> AuthService:
    return AuthService(session, get_token_issuer(), get_settings(), get_apple_verifier())


def get_audit(session: DbSession) -> AuditService:
    return AuditService(session)


def get_wallet_service(session: DbSession) -> WalletService:
    return WalletService(session, AuditService(session))


def get_byok_service(session: DbSession) -> BYOKService:
    # ADR-033: BYOK validates the key of the ACTIVE provider via the LLMClient factory.
    return BYOKService(session, get_kms_client(), get_llm_client(), AuditService(session))


def get_subscription_service(session: DbSession) -> SubscriptionService:
    # ADR-029/TD-021: the StoreKit sync path is retired; SubscriptionService no longer needs the
    # verifier (only admin_grant remains, ADR-048/052). The shared verifier still serves token
    # purchase (get_token_purchase_service).
    audit = AuditService(session)
    return SubscriptionService(
        session,
        WalletService(session, audit),
        audit,
    )


def get_token_purchase_service(session: DbSession) -> TokenPurchaseService:
    return TokenPurchaseService(
        session,
        get_storekit_verifier(),
        WalletService(session, AuditService(session)),
    )


def get_adapty_webhook_service(session: DbSession) -> AdaptyWebhookService:
    audit = AuditService(session)
    return AdaptyWebhookService(
        session,
        WalletService(session, audit),
        audit,
        get_settings(),
    )


def get_admin_service(session: DbSession) -> AdminService:
    audit = AuditService(session)
    wallet = WalletService(session, audit)
    subscription = SubscriptionService(session, wallet, audit)
    return AdminService(session, wallet, audit, subscription)


def get_chats_service(session: DbSession) -> ChatsService:
    # ADR-038: chats depends on the workspaces service (read-only owns_workspace) to validate the
    # target workspace when PATCH /v1/chats/{id} re-binds a chat to a workspace.
    return ChatsService(
        ChatsRepository(session),
        WorkspacesService(WorkspacesRepository(session)),
    )


def get_profile_service(session: DbSession) -> ProfileService:
    return ProfileService(session)


def get_preferences_service(session: DbSession) -> PreferencesService:
    return PreferencesService(session)


def get_workspaces_service(session: DbSession) -> WorkspacesService:
    return WorkspacesService(WorkspacesRepository(session))


def get_orchestrator(session: DbSession) -> ChatOrchestrator:
    audit = AuditService(session)
    website = WebsiteService(session)
    return ChatOrchestrator(
        session=session,
        repo=ChatRepository(session),
        wallet=WalletService(session, audit),
        byok=BYOKService(session, get_kms_client(), get_llm_client(), audit),
        audit=audit,
        # ADR-033: inject the active provider's LLMClient (anthropic default | openai).
        anthropic_client=get_llm_client(),
        site_tools=SiteToolHandlers(session, website, audit),
        # ADR-026: global server-side tools (time.now) with the default SystemClock. Project-
        # independent — no WebsiteService/session-context, wired alongside site_tools.
        global_tools=GlobalToolHandlers(clock=SystemClock()),
        preferences=PreferencesService(session),
        # ADR-036: workspace context provider (instructions + knowledge files) for workspace chats.
        workspaces=WorkspacesService(WorkspacesRepository(session)),
    )


_hermes_backend_singleton: RuntimeBackend | None = None


def get_hermes_backend() -> RuntimeBackend:
    """Process-wide RuntimeBackend (ADR-046). MVP fixes DockerBackend (docker-py).

    Singleton so the docker-py client (and its connection) is reused across requests and the
    reaper. A future config-selected backend (Modal/Daytona) plugs in here without touching the
    manager/registry.
    """
    global _hermes_backend_singleton
    if _hermes_backend_singleton is None:
        _hermes_backend_singleton = DockerBackend(
            health_timeout_seconds=get_settings().hermes_health_timeout_seconds
        )
    return _hermes_backend_singleton


def get_hermes_manager(session: DbSession) -> HermesInstanceManager:
    """Wire the per-user Hermes runtime manager (ADR-046): registry + backend + KMS + settings.

    Per-session (like the other service factories) so registry writes land in the request/reaper
    transaction. The backend is a process-wide singleton; the KMS client reuses the BYOK KMS
    (ADR-003) — no new crypto infrastructure.
    """
    return HermesInstanceManager(
        session=session,
        registry=HermesInstanceRegistry(session),
        backend=get_hermes_backend(),
        kms=get_kms_client(),
        settings=get_settings(),
    )


def get_agent_proxy_service(session: DbSession) -> AgentProxyService:
    """Wire the agent-proxy service (ADR-045/047): manager + wallet + audit + settings.

    Per-session like the other service factories so the policy read, the wallet debit (on
    ``run.completed``) and the audit rows land in the same request transaction. The Hermes
    instance manager and KMS reuse the existing per-user runtime wiring (ADR-046).
    """
    audit = AuditService(session)
    return AgentProxyService(
        session=session,
        manager=get_hermes_manager(session),
        wallet=WalletService(session, audit),
        audit=audit,
        settings=get_settings(),
    )


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in get_settings().trusted_proxy_networks())


def client_ip(request: Request) -> str | None:
    """Resolve the real client IP, respecting a trusted reverse-proxy chain.

    The API runs behind a reverse-proxy / LB (07-deployment.md), so the socket peer is the
    proxy, not the client. We only honour X-Forwarded-For / X-Real-IP when the immediate peer
    is a configured trusted proxy; otherwise the headers are attacker-controlled and ignored.
    From a trusted XFF chain we take the (hop_count + 1)-th entry from the right — the last
    address inserted by infrastructure we do NOT control — never the spoofable left-most one.
    """
    peer = request.client.host if request.client is not None else None
    if peer is None or not _is_trusted_proxy(peer):
        # Request did not arrive via a trusted proxy: do not trust forwarding headers.
        return peer

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
        if hops:
            hop_count = max(get_settings().trusted_proxy_hop_count, 1)
            # The chain is: client, proxy1, ..., proxyN(=peer). Trust the rightmost
            # `hop_count` entries (our infra) and take the next one as the client.
            index = len(hops) - hop_count - 1
            if index < 0:
                index = 0
            return hops[index]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return peer
