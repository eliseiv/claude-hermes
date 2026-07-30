"""Application configuration from environment (pydantic-settings).

All secrets and tunables come from env / secret manager (05-security.md, 07-deployment.md).
No magic numbers in business code: limits and grant size are config-driven (ADR-006).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _redis_url_db(url: str) -> int | None:
    """Logical DB number encoded in a Redis URL path, or None when it cannot be determined.

    ``redis://host:6379/2`` → 2; a URL with no path → 0 (the Redis default). Returns None for
    anything unparseable or non-numeric (``unix://``, a ``?db=`` query form, a service-discovery
    scheme) so the isolation check that uses it can SKIP rather than reject a configuration it
    simply failed to read — a validator must not turn "I could not tell" into "you are wrong".
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    segment = path.lstrip("/")
    if not segment:
        return 0
    return int(segment) if segment.isdigit() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage ---
    # Runtime DSN — least-privilege role `app_rw` (ADR-053, durable append-only audit_logs:
    # INSERT,SELECT on audit_logs, no UPDATE/DELETE/TRUNCATE). Used by the api process.
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/claude_ios",
        alias="DATABASE_URL",
    )
    # Migration DSN — full-privilege role `app_migrate` (ADR-053): DDL incl. audit_logs schema
    # edits/rollbacks and trigger toggling. Used ONLY by the `migrate` job (alembic upgrade head),
    # never by the runtime api. Default mirrors database_url (local single-role `postgres`); in prod
    # it points at `app_migrate`. migrations/env.py falls back DATABASE_URL_MIGRATE -> DATABASE_URL.
    database_url_migrate: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/claude_ios",
        alias="DATABASE_URL_MIGRATE",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- LLM provider selection (ADR-033) ---
    # One provider per instance. Default "anthropic" → existing instances (claude-ios/avelyra)
    # are unchanged; "openai" activates the OpenAI Chat Completions path. The OpenAI clone is a
    # separate instance with LLM_PROVIDER=openai + OPENAI_* (07-deployment.md §Мульти-инстанс).
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")

    # --- Model allowlist per provider (ADR-034) ---
    # JSON object {model-id: displayName} of the models a user may pick on this instance. Parsed
    # by allowed_models() with the SAME shape rules as token_products() (str→non-empty-str only).
    # Default "{}" → empty allowlist → backward-compatible fallback to the single instance default
    # model (allowed_models()). Per-provider: only the active provider's raw is read. Not secrets.
    anthropic_models_raw: str = Field(default="{}", alias="ANTHROPIC_MODELS")
    openai_models_raw: str = Field(default="{}", alias="OPENAI_MODELS")

    # --- OpenAI (ADR-033; used only when LLM_PROVIDER=openai) ---
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    # Output budget per call (parity with ANTHROPIC_MAX_TOKENS=16000).
    openai_max_tokens: int = Field(default=16000, alias="OPENAI_MAX_TOKENS")
    openai_timeout_seconds: float = Field(default=120.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES")
    # BYOK active model reported when keyStatus=valid on an OpenAI instance (ADR-016/ADR-033 §7).
    openai_byok_default_model: str = Field(default="gpt-4o", alias="OPENAI_BYOK_DEFAULT_MODEL")

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    # ADR-025: output budget per call. Raised 4096→16000 so code/file generation (several
    # files.write with full content) is not truncated by max_tokens. Stays non-streaming; 16000
    # is below the SDK non-streaming guard. Per-instance in .env (applied to every deploy instance).
    anthropic_max_tokens: int = Field(default=16000, alias="ANTHROPIC_MAX_TOKENS")
    # ADR-025: raised 60→120 to avoid a false 502 timeout on a long non-streaming generation at
    # max_tokens=16000. Configurable; still well below the SDK non-streaming guard.
    anthropic_timeout_seconds: float = Field(default=120.0, alias="ANTHROPIC_TIMEOUT_SECONDS")
    anthropic_max_retries: int = Field(default=2, alias="ANTHROPIC_MAX_RETRIES")
    # ADR-016: active model reported in BYOK responses when keyStatus=valid. Defaults to a
    # current Claude model; configurable via env. Not a secret (model name).
    byok_default_model: str = Field(default="claude-sonnet-4-6", alias="BYOK_DEFAULT_MODEL")

    # --- JWT (RS256, 05-security.md, Q-005-1 default own issuer) ---
    jwt_jwks_url: str = Field(default="", alias="JWT_JWKS_URL")
    jwt_issuer: str = Field(default="", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="", alias="JWT_AUDIENCE")
    # Optional static public key (PEM) fallback when JWKS endpoint is not configured.
    jwt_public_key: str = Field(default="", alias="JWT_PUBLIC_KEY")
    jwks_cache_ttl_seconds: int = Field(default=300, alias="JWT_JWKS_CACHE_TTL")

    # --- Embedded auth-issuer (ADR-018, modules/auth) ---
    # Private signing key (RS256). SECRET: never in repo/image/logs (redaction). Provided as a
    # PEM file path (preferred in prod: mounted secret) or as a PEM string with \n-escaping in
    # env. Path takes priority. Absent => issuer endpoints return 503 (verify-only still works).
    jwt_private_key: str = Field(default="", alias="JWT_PRIVATE_KEY")
    jwt_private_key_path: str = Field(default="", alias="JWT_PRIVATE_KEY_PATH")
    # Public key file path (alongside the existing PEM-string JWT_PUBLIC_KEY; path takes priority).
    jwt_public_key_path: str = Field(default="", alias="JWT_PUBLIC_KEY_PATH")
    # Key id placed in the JWT header / JWKS (key rotation groundwork, not MVP).
    jwt_kid: str = Field(default="", alias="JWT_KID")
    # Access-token TTL 1h, refresh-token TTL 30d (ADR-018 §5).
    auth_access_ttl_seconds: int = Field(default=3600, alias="AUTH_ACCESS_TTL_SECONDS")
    auth_refresh_ttl_seconds: int = Field(default=2592000, alias="AUTH_REFRESH_TTL_SECONDS")
    # Per-IP rate limit on /v1/auth/* (anti-abuse mass registration).
    auth_rate_limit_per_ip: int = Field(default=10, alias="AUTH_RATE_LIMIT_PER_IP")
    # Toggle GET /v1/auth/jwks (public, non-secret). Default true.
    auth_jwks_enabled: bool = Field(default=True, alias="AUTH_JWKS_ENABLED")
    # TD-013: background cleanup of auth_refresh_tokens (reaper pattern, ADR-046 §5). Poll interval
    # (default 1h) and the grace period (default 7d) kept for used/revoked rows so recently-rotated
    # tokens stay available to reuse-detect before deletion. Expired rows are deleted regardless of
    # grace. State lives in the DB → survives restart. No migration; auth contract unchanged.
    auth_refresh_cleanup_interval_seconds: int = Field(
        default=3600, alias="AUTH_REFRESH_CLEANUP_INTERVAL_SECONDS"
    )
    auth_refresh_cleanup_grace_seconds: int = Field(
        default=604800, alias="AUTH_REFRESH_CLEANUP_GRACE_SECONDS"
    )

    # --- KMS (envelope encryption, ADR-003, Q-002-1) ---
    kms_key_id: str = Field(default="", alias="KMS_KEY_ID")
    # Local fallback master key (base64, 32 bytes) for non-cloud envs; prod uses real KMS.
    kms_local_master_key: str = Field(default="", alias="KMS_LOCAL_MASTER_KEY")

    # --- App Store (Q-007-1) ---
    appstore_environment: str = Field(default="sandbox", alias="APPSTORE_ENVIRONMENT")
    appstore_bundle_id: str = Field(default="", alias="APPSTORE_BUNDLE_ID")
    appstore_root_cert_dir: str = Field(default="", alias="APPSTORE_ROOT_CERT_DIR")

    # --- Sign in with Apple (ADR-043, modules/auth Phase 6) ---
    # Apple OIDC identity-token verification for POST /v1/auth/apple. Native Sign in with Apple
    # only (aud = app bundle id); Services ID / web-flow is out of scope (Q-043-1). Values are
    # env (not secrets except APPLE_TEST_SECRET) and per-instance, like APPSTORE_BUNDLE_ID.
    apple_oidc_issuer: str = Field(default="https://appleid.apple.com", alias="APPLE_OIDC_ISSUER")
    apple_jwks_url: str = Field(
        default="https://appleid.apple.com/auth/keys", alias="APPLE_JWKS_URL"
    )
    # Expected `aud` = app bundle id. Empty => fall back to APPSTORE_BUNDLE_ID
    # (apple_audience_resolved()); both empty => Apple sign-in "not configured" => 503.
    apple_audience: str = Field(default="", alias="APPLE_AUDIENCE")
    # test-mode (ADR-043 §2): env-gated HS256 identity tokens for hermetic tests (no Apple infra).
    # Default false => prod fail-closed RS256 verification is unchanged. Active ONLY when
    # apple_test_mode is true AND apple_test_secret is non-empty; HS256 outside test-mode => 401
    # (no alg-confusion). The secret is redaction-allowlisted (`*secret*`) and never logged.
    apple_test_mode: bool = Field(default=False, alias="APPLE_TEST_MODE")
    apple_test_secret: str = Field(default="", alias="APPLE_TEST_SECRET")

    # --- StoreKit test-mode (TD-007, 09-e2e-testing.md §2; test/CI only) ---
    # Env-gated HS256 test transactions for e2e (no Apple infra). Default false => prod
    # fail-closed real JWS verification is unchanged. Active ONLY when storekit_test_mode is
    # true AND storekit_test_secret is non-empty. The secret is redaction-allowlisted and
    # never logged (05-security.md).
    storekit_test_mode: bool = Field(default=False, alias="STOREKIT_TEST_MODE")
    storekit_test_secret: str = Field(default="", alias="STOREKIT_TEST_SECRET")

    # --- Billing (ADR-006) ---
    subscription_credits_per_period: int = Field(
        default=1000, alias="SUBSCRIPTION_CREDITS_PER_PERIOD"
    )

    # --- Adapty subscription webhook (ADR-029, billing-adapty/07) ---
    # Isolated static bearer secret for POST /v1/billing/adapty/webhook. Set by the operator in
    # the Adapty UI; compared constant-time (hmac.compare_digest). Separate from JWT / admin /
    # KMS / preview secrets and per-instance (ADR-017). Empty (default) => the endpoint returns
    # 500 (misconfiguration); a blank secret never authenticates any presented token.
    adapty_webhook_secret: str = Field(default="", alias="ADAPTY_WEBHOOK_SECRET")
    # JSON object vendor_product_id -> tokens. Source of truth for the per-product grant tier on
    # subscription_started/renewed. Parsed by adapty_product_tokens() (same shape as
    # token_products()). Malformed/non-object => {} => every product falls back to the fixed grant.
    adapty_product_tokens_raw: str = Field(default="{}", alias="ADAPTY_PRODUCT_TOKENS")
    # Fixed fallback grant (tokens) used when vendor_product_id is absent from the tier map.
    # Isolated from SUBSCRIPTION_CREDITS_PER_PERIOD so the Adapty path is calibrated independently
    # (ADR-029 §5); defaults coincide (1000) for predictability.
    adapty_subscription_tokens_grant: int = Field(
        default=1000, alias="ADAPTY_SUBSCRIPTION_TOKENS_GRANT"
    )

    # --- Token purchase (ADR-015, token-purchase/03) ---
    # Server-side mapping consumable productId -> credits (JSON object). Source of truth for
    # how many credits a token-package purchase grants; never taken from the client body
    # (BR-TP-1 anti-tamper). Example: {"tokens_1500":1500,"tokens_600":600,"tokens_250":250,
    # "tokens_100":100}. Empty default => no products configured (every purchase 422 until set).
    token_products_raw: str = Field(default="{}", alias="TOKEN_PRODUCTS")

    # --- Admin auth (ADR-009, ADM-1) ---
    # Isolated admin secret (X-Admin-Token). High-entropy (>= 32 bytes), only via secret
    # manager / env, never in code/repo/image. Not shared with JWT/KMS/ANTHROPIC/PREVIEW
    # secrets. ADMIN_API_SECRET_PREV is the previous secret kept valid during rotation
    # (grace period); both compared constant-time. Empty (unset) secrets never match.
    admin_api_secret: str = Field(default="", alias="ADMIN_API_SECRET")
    admin_api_secret_prev: str = Field(default="", alias="ADMIN_API_SECRET_PREV")
    admin_rate_limit_per_min: int = Field(default=10, alias="ADMIN_RATE_LIMIT_PER_MIN")
    # Body size limit for admin endpoints (<= 8 KB, ADR-009 §6).
    admin_size_limit_body: int = Field(default=8 * 1024, alias="ADMIN_SIZE_LIMIT_BODY")

    # --- Client API-KEY auth (ADR-044) ---
    # Single trusted CLIENT key (X-API-Key) authenticating every user-facing /v1/* request of the
    # Hermes-integration client contour. High-entropy (>= 32 bytes), only via secret manager / env,
    # never in code/repo/image. Compared constant-time (hmac.compare_digest); an empty/unset value
    # never matches. High-privilege secret (knowledge of it = acting as ANY X-User-Id) — under
    # redaction (X-API-Key denylist), separate from JWT/KMS/ADMIN/PREVIEW secrets and per-instance
    # (ADR-017). CLIENT_API_KEY_PREV is the previous key kept valid during rotation (grace period);
    # both are compared constant-time, any match is accepted (ADR-044 §1, mirrors ADR-009 §5).
    client_api_key: str = Field(default="", alias="CLIENT_API_KEY")
    client_api_key_prev: str = Field(default="", alias="CLIENT_API_KEY_PREV")

    # --- Website builder / preview (ADR-010, ADR-011, WB-2) ---
    # Isolated HMAC secret for signed preview URLs. Separate from JWT/KMS/ADMIN secrets.
    preview_url_secret: str = Field(default="", alias="PREVIEW_URL_SECRET")
    preview_url_ttl_seconds: int = Field(default=900, alias="PREVIEW_URL_TTL_SECONDS")
    preview_max_file_bytes: int = Field(default=1024 * 1024, alias="PREVIEW_MAX_FILE_BYTES")
    preview_max_project_bytes: int = Field(
        default=10 * 1024 * 1024, alias="PREVIEW_MAX_PROJECT_BYTES"
    )
    preview_max_files: int = Field(default=200, alias="PREVIEW_MAX_FILES")
    # Guard against an infinite server-side tool loop (ADR-011 §2).
    max_server_tool_rounds: int = Field(default=16, alias="MAX_SERVER_TOOL_ROUNDS")
    # PUBLIC service host (not a secret; already in Traefik Host labels and .env.prod.example,
    # ADR-017). Read here only to build the ABSOLUTE site.preview URL so the model copies it
    # verbatim instead of hallucinating a host (ADR-031). Empty => relative fallback (dev).
    service_domain: str = Field(default="", alias="SERVICE_DOMAIN")

    # --- Trusted reverse-proxy (X-Forwarded-For parsing, 07-deployment.md) ---
    # API runs behind a reverse-proxy / LB (TLS termination). Only trust XFF/X-Real-IP
    # when the peer is a known proxy; otherwise the header is spoofable. Empty list =>
    # never trust forwarding headers, always use the socket peer (safe default).
    trusted_proxy_ips: str = Field(default="", alias="TRUSTED_PROXY_IPS")
    # Number of trusted proxy hops in front of the app (chained LB/CDN). The client IP is
    # taken (hop_count + 1) entries from the right of X-Forwarded-For. Default 1.
    trusted_proxy_hop_count: int = Field(default=1, alias="TRUSTED_PROXY_HOP_COUNT")

    # --- Rate limits (Q-003-1 defaults, TD-004) ---
    rate_limit_chat_per_user: int = Field(default=30, alias="RATE_LIMIT_CHAT_PER_USER")
    rate_limit_chat_per_device: int = Field(default=60, alias="RATE_LIMIT_CHAT_PER_DEVICE")
    rate_limit_chat_per_ip: int = Field(default=120, alias="RATE_LIMIT_CHAT_PER_IP")
    rate_limit_other_per_user: int = Field(default=60, alias="RATE_LIMIT_OTHER_PER_USER")
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")

    # --- Size limits in bytes (Q-003-2 defaults, TD-004) ---
    size_limit_body: int = Field(default=512 * 1024, alias="SIZE_LIMIT_BODY")
    size_limit_message: int = Field(default=32 * 1024, alias="SIZE_LIMIT_MESSAGE")
    size_limit_context: int = Field(default=64 * 1024, alias="SIZE_LIMIT_CONTEXT")
    size_limit_tool_result: int = Field(default=256 * 1024, alias="SIZE_LIMIT_TOOL_RESULT")
    size_limit_api_key: int = Field(default=4 * 1024, alias="SIZE_LIMIT_API_KEY")

    # --- Inline multimodal attachments (ADR-020, 05-security.md, Q-020-2 defaults) ---
    # Inline base64 attachments are accepted only in the first user message-step of
    # /v1/chat/run. All limits are enforced BEFORE base64 decoding to bound memory use
    # (decoded size ≈ 3/4 of the base64 length). The mediaType allowlist is fixed in code
    # (schemas/chat.py, Q-020-1 governs extension), not env-driven.
    attachment_max_count: int = Field(default=10, alias="ATTACHMENT_MAX_COUNT")
    # Per-attachment decoded-byte ceiling, split by class: image vs document (PDF).
    attachment_max_bytes_image: int = Field(
        default=5 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES_IMAGE"
    )
    attachment_max_bytes_document: int = Field(
        default=8 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES_DOCUMENT"
    )
    # Combined decoded-byte ceiling across all attachments in a request.
    attachment_total_bytes: int = Field(default=10 * 1024 * 1024, alias="ATTACHMENT_TOTAL_BYTES")
    # PDF page-count guard (anti decompression/structure bomb) via pypdf.
    attachment_pdf_max_pages: int = Field(default=100, alias="ATTACHMENT_PDF_MAX_PAGES")
    # Raised transport body limit applied ONLY to the /v1/chat/run route (other routes keep
    # size_limit_body). Inline base64 of large files exceeds the general ≤512KB cap.
    attachment_request_body_limit: int = Field(
        default=12 * 1024 * 1024, alias="ATTACHMENT_REQUEST_BODY_LIMIT"
    )

    # --- Workspaces (рабочие пространства) knowledge files (ADR-036 §4/§6) ---
    # Limits for workspace_files (own BYTEA table; ADR-036 §4, TD-027). All defaults are the
    # values fixed in ADR-036 (08 MB per file = the document-cap; 32 MB total per workspace; 20
    # files per workspace). WORKSPACE_CONTEXT_MAX_CHARS bounds the total injected extracted_text
    # (ADR-036 §6) — images are bounded by file count/size, not by this char limit.
    workspace_file_max_count: int = Field(default=20, alias="WORKSPACE_FILE_MAX_COUNT")
    workspace_file_max_bytes: int = Field(default=8 * 1024 * 1024, alias="WORKSPACE_FILE_MAX_BYTES")
    workspace_files_total_bytes: int = Field(
        default=32 * 1024 * 1024, alias="WORKSPACE_FILES_TOTAL_BYTES"
    )
    workspace_context_max_chars: int = Field(default=200_000, alias="WORKSPACE_CONTEXT_MAX_CHARS")
    # Raised transport body limit applied ONLY to the workspace files collection path
    # (/v1/workspaces/{id}/files). Inline base64 of a knowledge file (≤ WORKSPACE_FILE_MAX_BYTES
    # = 8 MB) exceeds the general ≤512KB size_limit_body once base64-inflated (~4/3) + JSON
    # envelope. 12 MB covers base64(8MB)≈10.67MB with headroom. Other routes keep size_limit_body.
    # (ADR-060). Independent from attachment_request_body_limit (chat/run) by design.
    workspace_request_body_limit: int = Field(
        default=12 * 1024 * 1024, alias="WORKSPACE_REQUEST_BODY_LIMIT"
    )

    # --- DB connection pool (02-tech-stack.md, sized for ~10k users / 2-3 replicas) ---
    # Per-process pool. Effective max conns ≈ (pool_size + max_overflow) * workers * replicas;
    # keep below Postgres max_connections. architect documents the sizing math in docs.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: float = Field(default=30.0, alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, alias="DB_POOL_RECYCLE")

    # --- Session (Q-001-1) ---
    session_soft_ttl_seconds: int = Field(default=24 * 3600, alias="SESSION_SOFT_TTL_SECONDS")

    # --- Wallet ---
    wallet_last_transactions: int = Field(default=20, alias="WALLET_LAST_TRANSACTIONS")

    # --- Policy cache ---
    policy_cache_ttl_seconds: int = Field(default=5, alias="POLICY_CACHE_TTL_SECONDS")

    # --- API documentation (08-api-documentation.md, R7) ---
    # Toggles /docs, /redoc, /openapi.json. Default true (dev/CI/staging). Recommended
    # false in prod so the API surface is not publicly exposed (05-security.md).
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")

    # --- Hermes runtime (per-user agent containers, ADR-046, 07-deployment.md) ---
    # Docker image + pinned tag of the Hermes agent (NOT `latest`, for reproducibility). Empty
    # default => provisioning fails fast (misconfiguration) rather than pulling an unknown image.
    # Per-instance. Not a secret.
    hermes_image: str = Field(default="", alias="HERMES_IMAGE")
    # Dedicated docker network connecting the control plane to Hermes instances. Instances do NOT
    # publish a host port — access is only from this network; addressing is by container DNS name.
    hermes_docker_network: str = Field(default="hermes-net", alias="HERMES_DOCKER_NETWORK")
    # Host root path for per-user HERMES_HOME volumes (mounted to /opt/data in the instance). The
    # volume survives hibernation (stop/start). Per-instance.
    hermes_volume_root: str = Field(default="/opt/data/hermes", alias="HERMES_VOLUME_ROOT")
    # Safe toolset written to the instance config.yaml (platform_toolsets.api_server). Comma-
    # separated; default excludes terminal/browser/code_execution/computer_use (05-security.md).
    # Configurable (groundwork for tiers). Parsed by hermes_default_toolset().
    hermes_default_toolset_raw: str = Field(
        default="web,file,vision,skills,todo", alias="HERMES_DEFAULT_TOOLSET"
    )
    # Hibernation threshold: a container whose last_active_at is older is stopped by the reaper
    # (stop_idle). Woken on demand by ensure_running. Default 30 min.
    hermes_idle_timeout_seconds: int = Field(default=1800, alias="HERMES_IDLE_TIMEOUT_SECONDS")
    # Reaper poll interval (lifespan background task). Independent of the idle threshold; default
    # 5 min. The reaper survives process restarts (state lives in hermes_instances, not memory).
    hermes_reaper_interval_seconds: int = Field(default=300, alias="HERMES_REAPER_INTERVAL_SECONDS")
    # LLM provider configured INSIDE the Hermes instance, written to config.yaml `model.provider`
    # (ADR-055; the image resolves the provider from config.yaml, NOT from env). Independent of our
    # LLM_PROVIDER (ADR-033). MUST be a CONCRETE provider from the image allowlist
    # (HERMES_PROVIDER_ALLOWLIST) and NOT `auto` (auto defaults to openrouter base_url → 401).
    # `openai` is invalid (no direct provider — use openrouter/custom). Default anthropic.
    # Validated fail-fast at provisioning.
    hermes_llm_provider: str = Field(default="anthropic", alias="HERMES_LLM_PROVIDER")
    # Service LLM API key supplied to the instance (mapped to the provider's key-env via
    # HERMES_PROVIDER_KEY_ENV, ADR-055 §4). SECRET — never logged (redaction `*key*`). Empty =>
    # provisioning fails fast.
    hermes_llm_api_key: str = Field(default="", alias="HERMES_LLM_API_KEY")
    # BARE model name of the Hermes instance (ADR-055 §3), e.g. `claude-3-5-haiku-latest` — WITHOUT
    # a provider prefix. The control plane assembles config.yaml `model.default` =
    # "<HERMES_LLM_PROVIDER>/<HERMES_MODEL>". The image ignores env `LLM_MODEL` → model is only set
    # via config.yaml. Empty => provisioning fails fast (empty model = the "Model: (empty)" bug).
    hermes_model: str = Field(default="", alias="HERMES_MODEL")
    # base_url for the instance LLM endpoint → config.yaml `model.base_url` (NOT env, ADR-055 §4).
    # REQUIRED for providers in HERMES_PROVIDERS_REQUIRING_BASE_URL (custom/azure-foundry); optional
    # for lmstudio; leave empty for the rest (the base_url line is then omitted → image default).
    hermes_llm_base_url: str = Field(default="", alias="HERMES_LLM_BASE_URL")
    # Per-instance API_SERVER_KEY length in bytes (CSPRNG). >=16 chars after base64url encoding;
    # 32 bytes ⇒ 43-char token (ADR-046 §1). Configurable, never below 16 bytes.
    hermes_api_key_bytes: int = Field(default=32, alias="HERMES_API_KEY_BYTES")
    # TD-031: max age of a `provisioning` row before ensure_running treats it as stale and replays
    # (deprovision + provision) instead of using the incomplete row (endpoint=NULL/DNS-fallback). A
    # crash between create_provisioning and mark_running leaves such a row; the threshold is well
    # above a normal provision (default 120s). A fresh provisioning row (younger) is left as-is
    # (concurrent-start, current behaviour). Configurable; per-instance.
    hermes_provisioning_stale_seconds: int = Field(
        default=120, alias="HERMES_PROVISIONING_STALE_SECONDS"
    )
    # ADR-056 §1: cold-start readiness gate. After `docker run`, provision polls the instance
    # GET /health until 200 before mark_running. Total budget (default 90s — above the ~30-40s
    # cold-start with margin) and poll interval (default 2s). Invariant (ADR-056 §3): the stale
    # threshold MUST exceed this budget so a live readiness-wait is not mistaken for a stale crash
    # residue (validated below, fail-fast). Per-instance.
    hermes_provision_ready_timeout_seconds: int = Field(
        default=90, alias="HERMES_PROVISION_READY_TIMEOUT_SECONDS"
    )
    hermes_provision_ready_interval_seconds: int = Field(
        default=2, alias="HERMES_PROVISION_READY_INTERVAL_SECONDS"
    )
    # ADR-056 §4(1): UID/GID passed to the Hermes container (HERMES_UID/HERMES_GID env). The image's
    # s6 stage2 chowns its /opt/data (the bind-mounted host volume) to these → the volume owner
    # matches the api process (which writes config.yaml), removing the reuse PermissionError. MUST
    # equal the api container's uid/gid (docker-compose); default 10001/10001 (05-security.md).
    hermes_uid: int = Field(default=10001, alias="HERMES_UID")
    hermes_gid: int = Field(default=10001, alias="HERMES_GID")
    # Health-probe timeout (GET /health of the instance), seconds.
    hermes_health_timeout_seconds: float = Field(default=5.0, alias="HERMES_HEALTH_TIMEOUT_SECONDS")
    # Proxy/SSE timeouts to a Hermes instance (ADR-045 §6). The non-streaming launch (POST /v1/runs)
    # uses a bounded timeout; the SSE relay (GET .../events) disables the READ timeout (long-lived
    # stream) but keeps connect/write bounded so a dead instance still fails fast.
    hermes_proxy_timeout_seconds: float = Field(default=30.0, alias="HERMES_PROXY_TIMEOUT_SECONDS")
    # CONNECT-phase cap for a non-streaming call to an instance, kept SEPARATE from the read/write
    # cap above. An instance lives on the docker network and is addressed by container DNS, so a
    # healthy connect is sub-second; giving connect the same 30s as read is what made the ADR-062
    # connect-only retry cost 3 × 30s + backoffs (~94s) — the very hang TD-040 measured, since a
    # retry is by construction only ever spent on the connect phase. Bounding connect separately is
    # what makes the CONNECT-RETRY CYCLE affordable — ≈ 34s (3 × 10 + 2 × 2) — without weakening the
    # read budget a slow-but-alive instance legitimately needs. That 34s bounds the retry cycle, NOT
    # the whole call: a connect that SUCCEEDS is followed by a read phase bounded separately by
    # HERMES_PROXY_TIMEOUT_SECONDS, so the mixed case (two refused connects, then a connect that
    # succeeds and goes silent) costs ≈ 10 + 2 + 10 + 2 + 30 ≈ 54s. All modes stay under the budget.
    hermes_connect_timeout_seconds: float = Field(
        default=10.0, alias="HERMES_CONNECT_TIMEOUT_SECONDS"
    )
    hermes_sse_connect_timeout_seconds: float = Field(
        default=10.0, alias="HERMES_SSE_CONNECT_TIMEOUT_SECONDS"
    )
    # END-TO-END budget of one proxied request to an instance: the row-lock wait, `ensure_running`
    # (provision / wake + the ADR-056/ADR-062 readiness poll), the HTTP call and every ADR-062
    # connect-retry share it. HERMES_PROXY_TIMEOUT_SECONDS bounds ONE attempt only, so the phases
    # used to stack: a ~90s readiness gate followed by a ~94s launch retry cycle (3 × 30s connect +
    # backoffs) = the ≥90s of silence measured in prod (TD-040). The deadline is taken once at the
    # entry point and threaded down, so it caps the SUM, not each phase. On exhaustion → 502
    # upstream_timeout. This is a SAFETY CAP, not the expected latency: with connect bounded
    # separately (HERMES_CONNECT_TIMEOUT_SECONDS) a wedged `running` instance answers in ≈34s (never
    # accepts TCP) / ≈30s (accepts, then silent) / ≈54s (mixed), far below this ceiling. It bounds
    # waiting on the INSTANCE — post-deadline cleanup and the untimed Docker calls (TD-041) are
    # outside it.
    # Invariant (validated below, fail-fast): >= readiness budget + TWO proxy timeouts — a LOWER
    # BOUND that keeps an ordinary cold start from being clipped, NOT a promise that every path fits
    # (07-deployment.md states the same): the /resume worst case is ready + 2 × (connect + proxy) =
    # 170 > 150, i.e. a cold start plus two maximally slow upstream calls IS clipped. Accepted
    # deliberately: that combination is a wedged instance, not a slow one. Lower this ONLY together
    # with HERMES_PROVISION_READY_TIMEOUT_SECONDS.
    hermes_launch_budget_seconds: float = Field(default=150.0, alias="HERMES_LAUNCH_BUDGET_SECONDS")

    # --- Agent usage-based billing (ADR-047, agent-proxy) ---
    # Credits charged per 1000 tokens for an agent run (/v1/agent/*). Conversion:
    #   amount = ceil(input/1000*CREDITS_PER_1K_INPUT + output/1000*CREDITS_PER_1K_OUTPUT)
    # with a floor of 1 credit on any non-zero usage (ADR-047 §2; credits are integers,
    # 03-data-model.md). Defaults are the tariff baseline (Q-047-1); per-instance, not secrets.
    credits_per_1k_input: float = Field(default=1.0, alias="CREDITS_PER_1K_INPUT")
    credits_per_1k_output: float = Field(default=5.0, alias="CREDITS_PER_1K_OUTPUT")

    # --- Hermes run-launch connect-only retry (ADR-062 §2, agent-proxy) ---
    # Defense-in-depth for POST /v1/runs: on a CONNECT-phase transport error (the request is
    # guaranteed not to have reached the server) _launch_run retries up to this many TOTAL attempts
    # with a fixed backoff between them. attempts=1 disables retry (single attempt). Only the
    # connect phase is retried — POST /v1/runs is NOT idempotent (no client key), so a post-send
    # error must never be retried (double-run risk, ADR-062 §2). Per-instance, not secrets.
    hermes_launch_retry_attempts: int = Field(default=3, alias="HERMES_LAUNCH_RETRY_ATTEMPTS")
    hermes_launch_retry_backoff_seconds: float = Field(
        default=2.0, alias="HERMES_LAUNCH_RETRY_BACKOFF_SECONDS"
    )

    # --- Agent debt reconciliation (ADR-051) ---
    # Gate for the agent-run debt reconciliation: partial-debit + wallets.debt on a shortfall
    # (WalletService.consume), clawback on grant, and the policy-gate debt_outstanding block.
    # Default true. When false, the ADR-047 §6 behaviour holds (full savepoint rollback on insuff.
    # balance, audit-only, no debt accounting, no policy block). The wallets.debt column exists
    # regardless of this flag (migration 0014). NOTE: this gates EMISSION only — the enum/achievable
    # set of blockReason ALWAYS includes debt_outstanding (agent-proxy/02, ADR-051 §4).
    agent_debt_reconcile_enabled: bool = Field(default=True, alias="AGENT_DEBT_RECONCILE_ENABLED")

    # --- Agent incremental billing + pause/resume (ADR-064) ---
    # Master gate for the incremental (per-step) agent-run billing, pause-at-zero (run.paused) and
    # resume (continuation) contour. Default false => the ADR-047 post-hoc behaviour holds
    # (single debit on run.completed, no agent_runs rows, resume unavailable) — a safe rollout.
    # When true: run() creates the root agent_runs row; stream_events() bills each usage.delta
    # (cumulative-owed-minus-charged, ADR-064 §1), stops at zero balance with a synthetic
    # run.paused (no debt, ADR-064 §3), and POST /v1/agent/runs/{runId}/resume continues the run
    # in the same Hermes session. Requires the Hermes image patch (usage.delta + hydrate endpoint,
    # ADR-064 §7); without it the flag-off post-hoc path is unaffected. Per-instance, not a secret.
    agent_incremental_billing_enabled: bool = Field(
        default=False, alias="AGENT_INCREMENTAL_BILLING_ENABLED"
    )

    # --- Agent run state snapshot (ADR-066, agent_run_snapshots) ---
    # NOT gated by AGENT_INCREMENTAL_BILLING_ENABLED: the snapshot writer runs on every relay.
    # Cap of agent_run_snapshots.result_text (the model text returned by
    # GET /v1/agent/runs/{runId}/state). Truncation is HEAD-preserving (the beginning is kept):
    # trimming the tail would break prefix stability, on which the upsert replay-guard relies
    # (ADR-066 §6). The full text always remains available through /events. Not a secret.
    agent_state_result_text_max_chars: int = Field(
        default=65536, alias="AGENT_STATE_RESULT_TEXT_MAX_CHARS"
    )
    # Throttle for persisting result_text while message.delta streams: at most one upsert per
    # interval. Terminal events (run.completed/run.failed/run.paused) and approval.request flush
    # IMMEDIATELY, bypassing the throttle (a delayed approval would break the UX). Lower value =>
    # fresher snapshot, higher write load. Not a secret.
    agent_state_flush_interval_seconds: float = Field(
        default=3.0, alias="AGENT_STATE_FLUSH_INTERVAL_SECONDS"
    )
    # Retention (ADR-066 §7): age after which the reaper clears result_text/pending_approval of
    # TERMINAL runs (completed/failed/cancelled/paused). The row is NOT deleted — /state keeps
    # returning status/usage/updatedAt; active runs are never touched. This is the only planned
    # user-content cleanup of the agent contour. Not a secret.
    agent_run_snapshot_ttl_days: int = Field(default=14, alias="AGENT_RUN_SNAPSHOT_TTL_DAYS")

    # --- Agent-run background consumer + broker /events (ADR-067, 07-deployment.md) -------------
    # Kill-switch of the broker model (ADR-067 §7). true: ONLY our background consumer subscribes to
    # Hermes (it bills, writes the snapshot and the terminal status), and the client's GET /events
    # reads downstream from Redis. false: the previous direct client→Hermes relay with billing on
    # that path — which means a run nobody subscribes to is again neither billed nor ever finalized
    # (TD-037), so false is an emergency rollback only. The /events contract is identical either
    # way. Temporary dual path — TD-038. Not a secret.
    agent_run_consumer_enabled: bool = Field(default=True, alias="AGENT_RUN_CONSUMER_ENABLED")
    # TTL of the Redis lease that owns the upstream subscription (agent:run:{runId}:lease) across
    # gunicorn workers (ADR-067 §4). Too low ⇒ needless takeovers; too high ⇒ a long pause before
    # another worker picks a dropped run up. A takeover cannot double-charge (ledger idempotency
    # runId:step / runId). Not a secret.
    agent_run_consumer_lease_ttl_seconds: int = Field(
        default=30, alias="AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS"
    )
    # How often a live consumer renews its lease (ADR-067 §4). Must be well below the TTL (the doc's
    # guidance is about a third); the validator enforces only the correctness boundary — at or above
    # the TTL the lease would always expire before it is renewed. Not a secret.
    agent_run_consumer_lease_renew_seconds: int = Field(
        default=10, alias="AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS"
    )
    # Liveness stamp period written by the consumer's SUPERVISOR into
    # agent_run_snapshots.consumer_heartbeat_at (migration 0020). Stamped ONLY on confirmed progress
    # of the worker task (ADR-067 §6.1), never on a bare schedule — otherwise liveness would be the
    # consumer's own say-so. MUST stay well below AGENT_RUN_ORPHAN_TIMEOUT_SECONDS (validated): a
    # heartbeat slower than the orphan threshold would have the sweep finalize live runs.
    # ⚠️ Written as a SEPARATE single-column UPDATE — the snapshot upsert is forbidden here (it
    # writes updated_at unconditionally, which is the client's staleness detector). Not a secret.
    agent_run_consumer_heartbeat_seconds: int = Field(
        default=30, alias="AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS"
    )
    # TCP keep-alive of the upstream subscription socket (ADR-067 §6.2): idle before the first
    # probe / interval between probes / probe count ⇒ a dead peer is detected in ≈90s at defaults.
    # ⚠️ httpx has no "enable keep-alive" knob — these must be passed as explicit socket_options
    # (SO_KEEPALIVE + TCP_KEEPIDLE/TCP_KEEPINTVL/TCP_KEEPCNT); without that the Linux default
    # tcp_keepalive_time=7200s applies and the detector is fictional (a lost peer would surface only
    # after AGENT_RUN_MAX_DURATION_SECONDS). Not secrets.
    agent_run_upstream_tcp_keepidle_seconds: int = Field(
        default=60, alias="AGENT_RUN_UPSTREAM_TCP_KEEPIDLE_SECONDS"
    )
    agent_run_upstream_tcp_keepintvl_seconds: int = Field(
        default=10, alias="AGENT_RUN_UPSTREAM_TCP_KEEPINTVL_SECONDS"
    )
    agent_run_upstream_tcp_keepcnt: int = Field(default=3, alias="AGENT_RUN_UPSTREAM_TCP_KEEPCNT")
    # Stall threshold of OUR OWN processing (ADR-067 §6.1): the worker sitting in the `processing`
    # beacon state longer than this makes the supervisor stop the heartbeat, drop the lease and
    # CANCEL the worker. ⚠️ Distinct from upstream silence: waiting for events (`awaiting_upstream`)
    # is not a liveness violation and may last hours — this threshold addresses a wedged DB write /
    # deadlock / a hang inside consume. Without it liveness would be self-declared: independent
    # lease and heartbeat loops would keep stamping while processing was hung, and the MAX_DURATION
    # timer inside the same coroutine would never fire. Not a secret.
    agent_run_processing_stall_seconds: int = Field(
        default=120, alias="AGENT_RUN_PROCESSING_STALL_SECONDS"
    )
    # How long the upstream subscription may hold with ZERO bytes read (ADR-067 §6.4.2 inert-
    # subscription guard); the beacon stays `connecting` for that whole time. Exceeded ⇒ supervisor
    # drops lease/heartbeat and cancels the worker.
    # ⚠️ The value is NOT derived from the measured 0.184s first-content latency: the captures show
    # the image emits no structural run.queued/run.running, so the first event is already content
    # (after the first LLM call), and the WORST case (long reasoning, large context, a tool first)
    # is UNMEASURED — Q-067-14. Chosen with margin (×27 of the 6.7s cold start) because the error
    # costs are asymmetric: a false positive kills a WORKING run, a false negative only lets an
    # inert subscription live longer. ⚠️ Not a silence threshold: past the first byte the guard is
    # inert (bytes_read > 0), so the retracted idle-timeout does not return this way. Not a secret.
    agent_run_first_byte_stall_seconds: int = Field(
        default=180, alias="AGENT_RUN_FIRST_BYTE_STALL_SECONDS"
    )
    # Ceiling on a run's lifetime from create_running, applied by the SUPERVISOR through cancelling
    # the worker (ADR-067 §6.3) — not by an internal timer, which would not fire inside a hung
    # coroutine; the reaper then finalizes the run.
    # ⚠️ A PRODUCT-VISIBLE limit ("a run longer than 2h is terminated, the completed part is
    # charged, status failed") and, after the idle-timeout was retracted, the ONLY time bound that
    # exists: the whole upper bound on the life of `running` is derived from it. Hence the fail-fast
    # `> 0` validation — 0 would silently remove that last guarantee. Value + telling iOS: Q-067-8.
    agent_run_max_duration_seconds: int = Field(
        default=7200, alias="AGENT_RUN_MAX_DURATION_SECONDS"
    )
    # Ring caps of one run's events in Redis (ADR-067 §3/§3.1). LTRIM fires on EITHER ceiling: an
    # event count alone is not enough (a single Hermes event has no size limit, so 5000 large
    # tool.completed blocks would be hundreds of MB for one run), and bytes alone would let a flood
    # of tiny events grow the list unboundedly. Overflow drops the HEAD of the stream; the client
    # then recovers the accumulated text via GET /state and is told so by run.truncated (Q-067-3).
    # Not secrets.
    agent_run_event_buffer_max: int = Field(default=5000, alias="AGENT_RUN_EVENT_BUFFER_MAX")
    agent_run_event_buffer_max_bytes: int = Field(
        default=8388608, alias="AGENT_RUN_EVENT_BUFFER_MAX_BYTES"
    )
    # TTL of the ring and the run's related keys (ADR-067 §3). RE-APPLIED on every event pipeline,
    # so it bounds IDLE time, not total run length — which is why 3600 legitimately sits below the
    # 7200 run ceiling (a run emitting events at least hourly keeps its ring alive). The keys are
    # ephemeral: losing them costs only the live stream — billing, status and /state live in
    # Postgres. Not a secret.
    agent_run_event_buffer_ttl_seconds: int = Field(
        default=3600, alias="AGENT_RUN_EVENT_BUFFER_TTL_SECONDS"
    )
    # Per-subscriber queue cap on the client /events stream (ADR-067 §3). Overflow DISCONNECTS the
    # slow subscriber (it reconnects and gets a replay); the consumer is never stopped — the
    # invariant is "a downstream failure must not affect upstream". Not a secret.
    agent_run_subscriber_queue_max: int = Field(default=500, alias="AGENT_RUN_SUBSCRIBER_QUEUE_MAX")
    # Max time a CLIENT /events stream may go without events while no live lease exists, after
    # which the stream is closed (ADR-067 §3.3). One of the five downstream close rules: a terminal
    # event may never appear in Redis at all (the consumer never started or died, the ring TTL
    # expired, Redis restarted), and the stream used to be closed by Hermes itself — under the
    # broker model we must close it, or the client hangs forever. Not a secret.
    agent_run_downstream_idle_timeout_seconds: int = Field(
        default=300, alias="AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS"
    )
    # Heartbeat staleness after which a run is considered orphaned (ADR-067 §5). A candidate must
    # satisfy ALL THREE conditions (no live lease; COALESCE(consumer_heartbeat_at, created_at) older
    # than this; Redis uptime >= the grace below, fail-closed). Not a secret.
    agent_run_orphan_timeout_seconds: int = Field(
        default=900, alias="AGENT_RUN_ORPHAN_TIMEOUT_SECONDS"
    )
    # Max runs finalized by one orphan sweep tick, PER WORKER (ADR-067 §5) — the reaper lives in
    # every worker's lifespan (4 of them), so the effective ceiling is 4 × this. A safety valve
    # against mass finalization; a tick that hits the cap is logged as an anomaly. Not a secret.
    agent_run_orphan_max_per_tick: int = Field(default=20, alias="AGENT_RUN_ORPHAN_MAX_PER_TICK")
    # Minimum Redis uptime_in_seconds (INFO server) that allows the orphan sweep to run at all
    # (ADR-067 §5). ⚠️ MANDATORY: a Redis restart wipes every lease at once, and without this the
    # sweep would take all active runs for orphans — charging early and marking WORKING runs failed
    # (after which the real _mark_terminal('completed') is a no-op, the transition being
    # conditional). FAIL-CLOSED: an INFO error or unreachable Redis means the sweep does not run.
    # Checked on server uptime rather than "age of the current connection" — redis.asyncio pools
    # connections, so there is no such thing as "the current" one. Must exceed the lease renew
    # period (validated), or the sweep could fire before live consumers re-took their leases. Not a
    # secret.
    agent_run_orphan_redis_grace_seconds: int = Field(
        default=120, alias="AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS"
    )
    # Logical Redis DB for agent-run keys (ring / channel / lease / seq / epoch), ADR-067 §3.5 —
    # operational isolation from rate limiting and idempotency marks, so that a FLUSHDB or a SCAN
    # sweep of one contour cannot touch the other. Validated to DIFFER from the DB in REDIS_URL:
    # equal values silently void exactly the isolation this setting exists for.
    # ⚠️ The default is DERIVED, not quoted: 07-deployment.md states the requirement (a separate
    # logical DB) but no number, and 1 is the only value consistent with the REDIS_URL default
    # (…/0). Pending architect confirmation. Not a secret.
    agent_run_redis_db: int = Field(default=1, alias="AGENT_RUN_REDIS_DB")
    # Budget for draining live consumers at worker shutdown (ADR-067 §6.1.1). The lifespan cancels
    # every registered consumer and waits this long for their §6.4 procedures (final flush, lease
    # release, audit) BEFORE the DB pool and Redis are closed — in the opposite order §6.4 could
    # neither flush nor release, and an orderly stop would degrade into an abrupt one.
    # NOT summed over the number of runs: the §6.4 procedures run concurrently, so this is a wall
    # clock bound for all of them together and fits inside gunicorn's --graceful-timeout 30.
    agent_run_shutdown_drain_seconds: float = Field(
        default=10.0, alias="AGENT_RUN_SHUTDOWN_DRAIN_SECONDS"
    )
    # How long POST /v1/agent/run waits for the consumer to report its upstream subscription before
    # answering 202 (ADR-067 §3, §6.1.1). Deliberately its OWN knob and deliberately SMALL — not
    # AGENT_RUN_FIRST_BYTE_STALL_SECONDS: the events stream has NO read timeout by §6.2, so a peer
    # that accepts the connection and never sends headers would otherwise hold the request open
    # indefinitely, and committing before the wait does not help with that.
    # ⚠️ Expiry is NOT fatal and NOT a correctness gate: under the broker model the client's
    # /events never reaches Hermes, so there is no race for the one-shot stream to lose. "Someone
    # is consuming this run" is guaranteed by the task registry (§6.1.1) and the reaper (§5); this
    # wait buys OBSERVABILITY — a failed subscription shows up now instead of after
    # AGENT_RUN_ORPHAN_TIMEOUT. On expiry the handler answers 202 and the consumer keeps going.
    agent_run_handshake_timeout_seconds: float = Field(
        default=15.0, alias="AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS"
    )
    # Budget for handing queued blocks to a client on the /events stream (ADR-067 §3.2.2). The
    # per-subscriber queue above measures HOW FAR BEHIND a client is; this measures HOW LONG we are
    # prepared to wait for it, which is the criterion the replay phase needs: the ring holds ten
    # times what the queue does (5000 against 500), so the reader hands the replay over waiting for
    # room rather than by depth, and a client too slow for that is dropped on TIME.
    # ⚠️ ONE value, TWO independent deadlines — the replay phase counts its own in the reader and
    # the normal drain counts its own in the writer, each locally, with no deadline shared between
    # tasks. A single shared counter was rejected for a reason worth keeping: a slow replay would
    # spend all of it and leave the drain nothing, discarding the terminal event exactly where
    # §3.2.2 gives the normal path priority. Honest cost of two: up to 2x the value in the worst
    # case ("slow replay, then slow drain"). Each deadline bounds ONE WAIT — progress resets it — so
    # a client that keeps draining is never cut off for merely having more to receive than the
    # budget covers in wall-clock terms.
    agent_run_subscriber_drain_seconds: float = Field(
        default=120.0, alias="AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS"
    )
    # How often the /events supervisor probes the close rules 4 and 5 (ADR-067 §3.2.2, TD-050).
    #
    # ⛔ Its OWN knob, and the reason is the defect this whole section was cleaning up: the period
    # used to be read from ``AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS`` — a property substituted by a
    # number that happened to be nearby. The two are unrelated: the lease TTL says how long a
    # consumer's claim on an upstream subscription survives without renewal, this says how quickly a
    # CLIENT stream notices that its run has ended. Tuning the former silently moved the latter, and
    # nobody adjusting a lease expects to have changed downstream closing latency.
    #
    # Default 10 s, chosen on two counts. It is the worst-case delay before a client is told the run
    # is over when no terminal event ever reaches Redis (the consumer died, the ring expired, Redis
    # was flushed) — the old effective value was 30 s, and a third of that is a plainly better
    # answer to "why is my stream still open". And it is cheap: one status query per open stream per
    # period, so at the ~15 concurrent streams a worker's pool allows it is ~1.5 queries/s per
    # worker, each on its own short session.
    #
    # ⚠️ It also sits comfortably above the probe's own 5 s deadline
    # (``broker._PERIODIC_PROBE_TIMEOUT_SECONDS``), so a wedged probe cannot turn the loop into
    # back-to-back probes — see the note in the validator for why that relation is NOT fail-fast.
    agent_run_subscriber_probe_seconds: float = Field(
        default=10.0, alias="AGENT_RUN_SUBSCRIBER_PROBE_SECONDS"
    )

    # --- Observability ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    metrics_scrape_token: str = Field(default="", alias="METRICS_SCRAPE_TOKEN")

    @model_validator(mode="after")
    def _validate_hermes_provision_invariant(self) -> Settings:
        """Fail-fast on the ADR-056 §3 invariant: stale threshold MUST exceed the ready budget.

        ``HERMES_PROVISIONING_STALE_SECONDS`` (TD-031 crash residue threshold) must be strictly
        greater than ``HERMES_PROVISION_READY_TIMEOUT_SECONDS`` (cold-start readiness budget) so a
        live readiness-wait (a `provisioning` row legitimately waiting up to the ready budget) is
        never mistaken for a stale crash residue and concurrently replayed. Validated at settings
        construction → a misconfiguration fails the process at startup, not at provision time.
        """
        if self.hermes_provisioning_stale_seconds <= self.hermes_provision_ready_timeout_seconds:
            raise ValueError(
                "HERMES_PROVISIONING_STALE_SECONDS "
                f"({self.hermes_provisioning_stale_seconds}) must be greater than "
                "HERMES_PROVISION_READY_TIMEOUT_SECONDS "
                f"({self.hermes_provision_ready_timeout_seconds}) — ADR-056 §3"
            )
        return self

    @model_validator(mode="after")
    def _validate_hermes_launch_budget_invariant(self) -> Settings:
        """Fail-fast: the end-to-end launch budget must cover a cold start plus one full attempt.

        A LOWER BOUND, not a guarantee that nothing is ever clipped. What it does guarantee: an
        ordinary cold start (~30-40s, ADR-056) plus the READ phase of both ``/resume`` upstream
        calls fits, so a merely slow instance is never failed with ``upstream_timeout`` while it was
        still booting. The factor TWO is the ``/resume`` path (ADR-064 §5): hydrate the session
        transcript, then launch the continuation, both under one deadline.

        What it does NOT cover: the connect phases of those two calls. The true ``/resume`` worst
        case is ``ready + 2 × (connect + proxy)`` = 170s at defaults, above the 150s default budget,
        so that combination IS clipped — accepted deliberately and stated the same way in
        07-deployment.md. Reaching it means a cold start followed by two maximally slow upstream
        calls, which describes a wedged instance rather than a slow one; failing it at the budget is
        the correct outcome. Rejecting a too-small budget at startup is the only place these knobs
        are visible together; at request time the truncation is indistinguishable from a dead
        instance.
        """
        minimum = self.hermes_provision_ready_timeout_seconds + (
            2 * self.hermes_proxy_timeout_seconds
        )
        if self.hermes_launch_budget_seconds < minimum:
            raise ValueError(
                f"HERMES_LAUNCH_BUDGET_SECONDS ({self.hermes_launch_budget_seconds}) must be >= "
                "HERMES_PROVISION_READY_TIMEOUT_SECONDS + 2 × HERMES_PROXY_TIMEOUT_SECONDS "
                f"({minimum}) so a cold start plus the two /resume upstream calls always fit"
            )
        return self

    @model_validator(mode="after")
    def _validate_agent_run_consumer_invariants(self) -> Settings:
        """Fail-fast on the ADR-067 settings that are only correct in relation to each other.

        Every check below has a failure mode that is SILENT at runtime — a misconfiguration does not
        error, it quietly finalizes live runs, charges them, or removes a guarantee. Startup is the
        only place these knobs are visible together (07-deployment.md documents them one row at a
        time, and each row looks reasonable alone), which is exactly the argument that produced the
        ADR-056 §3 and TD-040 invariants above.
        """
        # (1) MAX_DURATION > 0 — explicitly required fail-fast (ADR-067 §6.3). After the idle
        # timeout was retracted this is the ONLY bound on the life of `running`; 0 would silently
        # mean "no limit", which is admissible only as a deliberate Q-067-8 decision, not as a typo.
        if self.agent_run_max_duration_seconds <= 0:
            raise ValueError(
                "AGENT_RUN_MAX_DURATION_SECONDS must be > 0 — it is the only upper bound on the "
                "lifetime of a running agent run (ADR-067 §6.3)"
            )
        # (2) Every other knob is a period, a size or a count: non-positive values do not degrade
        # the contour, they disable the mechanism the setting names (a 0 ring cap keeps no events, a
        # 0 heartbeat period spins, a 0 per-tick cap sweeps nothing) while the config still reads as
        # if the feature were on.
        positive: dict[str, int] = {
            "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS": self.agent_run_consumer_lease_ttl_seconds,
            "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS": self.agent_run_consumer_lease_renew_seconds,
            "AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS": self.agent_run_consumer_heartbeat_seconds,
            "AGENT_RUN_UPSTREAM_TCP_KEEPIDLE_SECONDS": (
                self.agent_run_upstream_tcp_keepidle_seconds
            ),
            "AGENT_RUN_UPSTREAM_TCP_KEEPINTVL_SECONDS": (
                self.agent_run_upstream_tcp_keepintvl_seconds
            ),
            "AGENT_RUN_UPSTREAM_TCP_KEEPCNT": self.agent_run_upstream_tcp_keepcnt,
            "AGENT_RUN_PROCESSING_STALL_SECONDS": self.agent_run_processing_stall_seconds,
            "AGENT_RUN_FIRST_BYTE_STALL_SECONDS": self.agent_run_first_byte_stall_seconds,
            "AGENT_RUN_EVENT_BUFFER_MAX": self.agent_run_event_buffer_max,
            "AGENT_RUN_EVENT_BUFFER_MAX_BYTES": self.agent_run_event_buffer_max_bytes,
            "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": self.agent_run_event_buffer_ttl_seconds,
            "AGENT_RUN_SUBSCRIBER_QUEUE_MAX": self.agent_run_subscriber_queue_max,
            "AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS": (
                self.agent_run_downstream_idle_timeout_seconds
            ),
            "AGENT_RUN_ORPHAN_TIMEOUT_SECONDS": self.agent_run_orphan_timeout_seconds,
            "AGENT_RUN_ORPHAN_MAX_PER_TICK": self.agent_run_orphan_max_per_tick,
            # Grace 0 would mean "any uptime is fine", i.e. the Redis-restart protection this
            # setting exists for would be off while still appearing configured (ADR-067 §5).
            "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS": self.agent_run_orphan_redis_grace_seconds,
        }
        # A zero drain budget does not "skip waiting" — it cancels every consumer and closes the
        # pool underneath their §6.4 procedures, so no run gets a final flush, a released lease or
        # an audit record on an ORDERLY restart. Deploys are routine; this must not be a no-op.
        if self.agent_run_handshake_timeout_seconds <= 0:
            raise ValueError(
                "AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS must be > 0 "
                f"(got {self.agent_run_handshake_timeout_seconds}) — 0 would answer 202 without "
                "ever observing whether the consumer subscribed (ADR-067 §3)"
            )
        if self.agent_run_shutdown_drain_seconds <= 0:
            raise ValueError(
                "AGENT_RUN_SHUTDOWN_DRAIN_SECONDS must be > 0 "
                f"(got {self.agent_run_shutdown_drain_seconds}) — a zero budget closes the DB pool "
                "under the §6.4 shutdown procedures of live consumers (ADR-067 §6.1.1)"
            )
        # ⚠️ Only ``> 0`` is enforced, deliberately. The relation "probe deadline < period" holds for
        # the DEFAULT (5 s against 10 s) and is the right shape for production, but making it
        # fail-fast would forbid the cheap test scene this knob exists for — a 0.5 s period against
        # the 5 s deadline, which makes the deadline observable as 5.5 s versus 8.5 s instead
        # of as a 7-versus-10 s flake. Enforcing it would also mean enforcing a relation to a value
        # that is a module constant rather than a setting, which the validator cannot see.
        if self.agent_run_subscriber_probe_seconds <= 0:
            raise ValueError(
                "AGENT_RUN_SUBSCRIBER_PROBE_SECONDS must be > 0 "
                f"(got {self.agent_run_subscriber_probe_seconds}) — a zero period would spin the "
                "supervisor's probes into a hot loop against Postgres and Redis (ADR-067 §3.2.2)"
            )
        if self.agent_run_subscriber_drain_seconds <= 0:
            raise ValueError(
                "AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS must be > 0 "
                f"(got {self.agent_run_subscriber_drain_seconds}) — a zero budget gives a client "
                "no time to take what is already queued for it, so every stream would end by "
                "timing out on its own replay (ADR-067 §3.2.2)"
            )
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be > 0 (got {value}) — ADR-067")
        # (3) The lease must be renewed strictly before it expires (ADR-067 §4: "well below the
        # TTL, roughly a third"). At or above the TTL the lease lapses between renewals, so the run
        # is perpetually up for takeover and the "single upstream subscriber" property — on which
        # the ONE-SHOT Hermes stream depends — is lost.
        if self.agent_run_consumer_lease_renew_seconds >= self.agent_run_consumer_lease_ttl_seconds:
            raise ValueError(
                "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS "
                f"({self.agent_run_consumer_lease_renew_seconds}) must be < "
                "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS "
                f"({self.agent_run_consumer_lease_ttl_seconds}) — a lease renewed no sooner than "
                "it expires is never held (ADR-067 §4)"
            )
        # (4) A live consumer must be able to stamp several heartbeats within the orphan window.
        # Otherwise the sweep finalizes runs that are working: it charges them from the snapshot
        # cumulative and marks them failed, and the real terminal transition afterwards is a no-op
        # (conditional UPDATE) — money and status both wrong, with nothing logged as an error.
        if self.agent_run_consumer_heartbeat_seconds >= self.agent_run_orphan_timeout_seconds:
            raise ValueError(
                "AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS "
                f"({self.agent_run_consumer_heartbeat_seconds}) must be < "
                "AGENT_RUN_ORPHAN_TIMEOUT_SECONDS "
                f"({self.agent_run_orphan_timeout_seconds}) — otherwise the orphan sweep finalizes "
                "runs whose consumer is alive (ADR-067 §5)"
            )
        # (5) After a Redis restart every lease is gone at once and live consumers re-take theirs
        # within one renew period. A grace no longer than that lets the sweep run in the window
        # where nobody holds a lease yet — precisely the mass false finalization the grace exists to
        # prevent (ADR-067 §5).
        if self.agent_run_orphan_redis_grace_seconds <= self.agent_run_consumer_lease_renew_seconds:
            raise ValueError(
                "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS "
                f"({self.agent_run_orphan_redis_grace_seconds}) must be > "
                "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS "
                f"({self.agent_run_consumer_lease_renew_seconds}) — live consumers need at least "
                "one renew period to re-take their leases after a Redis restart (ADR-067 §5)"
            )
        # (6) The agent-run keys must not share a logical DB with rate limiting / idempotency
        # (ADR-067 §3.5). Equal values do not fail anywhere at runtime — they just mean a FLUSHDB
        # or SCAN sweep of one contour silently takes the other with it.
        if self.agent_run_redis_db < 0:
            raise ValueError(
                f"AGENT_RUN_REDIS_DB must be >= 0 (got {self.agent_run_redis_db}) — ADR-067 §3.5"
            )
        main_db = _redis_url_db(self.redis_url)
        if main_db is not None and main_db == self.agent_run_redis_db:
            raise ValueError(
                f"AGENT_RUN_REDIS_DB ({self.agent_run_redis_db}) must differ from the logical DB "
                f"of REDIS_URL ({main_db}) — the agent-run ring shares Redis with rate limiting "
                "and idempotency marks, and the whole point of the setting is that a FLUSHDB or "
                "SCAN of one contour cannot touch the other (ADR-067 §3.5)"
            )
        return self

    def token_products(self) -> dict[str, int]:
        """Parse TOKEN_PRODUCTS (JSON object productId->credits) into a validated mapping.

        Only string keys with positive-int credit values survive (ADR-015, BR-TP-1). A
        malformed JSON document or non-object yields an empty mapping (every purchase then
        fails 422), never a partial/ambiguous credit table. Pure (no I/O); cached via
        get_settings()'s lru_cache for the process lifetime.
        """
        import json

        try:
            parsed = json.loads(self.token_products_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    def adapty_product_tokens(self) -> dict[str, int]:
        """Parse ADAPTY_PRODUCT_TOKENS (JSON object vendor_product_id->tokens) (ADR-029 §5).

        Mirrors token_products(): only string keys with positive-int values survive (bool is a
        subclass of int and is excluded). A malformed JSON document or non-object yields an empty
        mapping, in which case every vendor_product_id falls back to
        adapty_subscription_tokens_grant. Pure (no I/O); cached via get_settings()'s lru_cache.
        """
        import json

        try:
            parsed = json.loads(self.adapty_product_tokens_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    def hermes_default_toolset(self) -> list[str]:
        """Parse HERMES_DEFAULT_TOOLSET (comma-separated) into a clean toolset list (ADR-046 §6).

        Whitespace is stripped and empty entries dropped, preserving order and de-duplicating.
        A blank/unset value falls back to the safe default ``[web, file, vision, skills, todo]``
        (no terminal/browser/code_execution/computer_use, 05-security.md). Pure (no I/O); cached
        via get_settings()'s lru_cache.
        """
        seen: dict[str, None] = {}
        for raw in self.hermes_default_toolset_raw.split(","):
            entry = raw.strip()
            if entry:
                seen.setdefault(entry, None)
        if not seen:
            return ["web", "file", "vision", "skills", "todo"]
        return list(seen.keys())

    def default_model(self) -> str:
        """Active instance default model (ADR-034 §1): the model used when none is selected.

        Provider-aware: ``openai_model`` when ``LLM_PROVIDER=openai``, otherwise ``anthropic_model``
        (the default). This is the model the active client falls back to
        (``settings.<provider>_model``) when ``create_message(model=None)`` — so it is, by
        construction, ALWAYS present in
        ``allowed_models()`` (the empty-allowlist fallback returns exactly this model; a non-empty
        allowlist without it has it prepended at the API layer — GET /v1/models).
        """
        if self.llm_provider.strip().lower() == "openai":
            return self.openai_model
        return self.anthropic_model

    def allowed_models(self) -> dict[str, str]:
        """Parse the active provider's model allowlist into a validated {id: displayName} mapping.

        Provider-aware (ADR-034 §1): reads ``openai_models_raw`` when ``LLM_PROVIDER=openai``, else
        ``anthropic_models_raw``. Same shape rules as ``token_products()``: only ``str`` keys with a
        non-empty ``str`` value survive (key stripped to a non-empty string; value a non-empty
        string after no transformation beyond the emptiness check). A malformed JSON document or a
        non-object yields an empty mapping.

        Backward-compatibility fallback: when the parsed result is empty, returns
        ``{default_model(): default_model()}`` — a single entry equal to the instance default model
        (displayName = id). So an unset allowlist reproduces the current behavior exactly (one
        model, the instance default).

        Invariant (ADR-034 §1): ``default_model()`` is ALWAYS present in the result. When a
        non-empty allowlist does NOT contain the default, the default is PREPENDED (displayName =
        id, first key) so it is always selectable and the §3 allowlist validation accepts it; the
        rest keep the allowlist insertion order. Pure (no I/O); cached via get_settings() lru_cache.
        """
        import json

        raw = (
            self.openai_models_raw
            if self.llm_provider.strip().lower() == "openai"
            else self.anthropic_models_raw
        )
        try:
            parsed = json.loads(raw or "{}")
        except (ValueError, json.JSONDecodeError):
            parsed = {}
        parsed_models: dict[str, str] = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if not isinstance(key, str):
                    continue
                stripped_key = key.strip()
                if not stripped_key:
                    continue
                # bool is a subclass of int (not str); the isinstance(str) check excludes it.
                if not isinstance(value, str) or not value:
                    continue
                parsed_models[stripped_key] = value
        default = self.default_model()
        if not parsed_models:
            # Empty allowlist → backward-compatible single default entry (displayName = id).
            return {default: default}
        if default in parsed_models:
            return parsed_models
        # Non-empty allowlist missing the default → prepend the default first (invariant §1),
        # keeping the allowlist's insertion order for the rest.
        return {default: default, **parsed_models}

    @staticmethod
    def _resolve_pem(path_value: str, string_value: str) -> str:
        """Resolve a PEM key: file path takes priority over the \\n-escaped string (ADR-018 §7).

        When a path is set it is read from disk verbatim (recommended prod: mounted secret, no
        escaping). Otherwise the env string value has literal ``\\n`` sequences turned into real
        newlines so a single-line .env value yields a valid multi-line PEM. Empty when neither is
        configured. Never logs the key material (redaction covers ``*key*``).
        """
        if path_value:
            with open(path_value, encoding="utf-8") as handle:
                return handle.read()
        if string_value:
            return string_value.replace("\\n", "\n")
        return ""

    def resolve_private_key(self) -> str:
        """Private RS256 signing key PEM, or '' if the issuer is not configured (=> 503)."""
        return self._resolve_pem(self.jwt_private_key_path, self.jwt_private_key)

    def resolve_public_key(self) -> str:
        """Public RS256 verification key PEM (used by JwtVerifier and the JWKS endpoint)."""
        return self._resolve_pem(self.jwt_public_key_path, self.jwt_public_key)

    def apple_audience_resolved(self) -> str:
        """Effective Apple `aud` for verification (ADR-043 §3).

        Returns ``apple_audience`` (stripped) if set, else ``appstore_bundle_id`` (stripped) as a
        fallback (if a bundle id is already configured for StoreKit it doubles as the Apple
        audience), else ``""``. An empty result means Apple sign-in is "not configured" — the
        router returns 503 (operational misconfiguration, not a client error). Pure (no I/O).
        """
        explicit = self.apple_audience.strip()
        if explicit:
            return explicit
        return self.appstore_bundle_id.strip()

    def normalized_service_domain(self) -> str:
        """Return SERVICE_DOMAIN as a bare host[:port] for the absolute preview URL (ADR-031).

        Strips a leading http(s):// scheme (case-insensitive) and surrounding slashes so the
        value is the same host regardless of how it is set (``broadnova.shop``,
        ``https://broadnova.shop`` or ``broadnova.shop/``). Returns '' when unset/blank, which
        the caller treats as "not configured" => relative fallback. Snapping the trailing slash
        guarantees the assembled URL has no double slash before ``/v1/``.
        """
        value = self.service_domain.strip()
        lowered = value.lower()
        if lowered.startswith("https://"):
            value = value[len("https://") :]
        elif lowered.startswith("http://"):
            value = value[len("http://") :]
        value = value.strip("/")
        return value

    def trusted_proxy_networks(self) -> tuple[_IpNetwork, ...]:
        """Parse TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs) into networks.

        Invalid entries are skipped. Empty/blank => empty tuple (never trust XFF).
        """
        networks: list[_IpNetwork] = []
        for raw in self.trusted_proxy_ips.split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
        return tuple(networks)


# Content-type allowlist for site_files (ADR-010, website-builder/05-security.md). Only these
# types may be stored and served by the preview endpoint. Fixed on the server (not configurable
# at runtime to keep the threat model deterministic; Q-010-2 leaves the exact list to architect).
PREVIEW_CONTENT_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "text/html",
        "text/css",
        "text/javascript",
        "application/json",
        "image/png",
        "image/jpeg",
        "image/svg+xml",
        "image/gif",
        "image/webp",
        "font/woff2",
        "text/plain",
    }
)


# --- Hermes instance LLM provider contract (ADR-055) ----------------------------------------------
# Closed-set allowlist of valid HERMES_LLM_PROVIDER values — source: the Hermes image
# cli-config.yaml.example. `auto` is in the image set but is FORBIDDEN for provisioning by the
# control plane (it defaults to the openrouter base_url → 401); fail-fast validation rejects it
# separately. `openai` is intentionally absent (no direct provider — OpenAI via openrouter/custom).
HERMES_PROVIDER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "auto",
        "openrouter",
        "nous",
        "nous-api",
        "anthropic",
        "openai-codex",
        "copilot",
        "gemini",
        "zai",
        "kimi-coding",
        "minimax",
        "minimax-cn",
        "huggingface",
        "nvidia",
        "xiaomi",
        "arcee",
        "ollama-cloud",
        "kilocode",
        "azure-foundry",
        "lmstudio",
        "custom",
    }
)

# Provider that is in the image allowlist but is FORBIDDEN for control-plane provisioning (ADR-055
# §2): `auto` revives the openrouter-default bug. A concrete provider is required.
HERMES_PROVIDER_FORBIDDEN: frozenset[str] = frozenset({"auto"})

# Explicit map provider → the container env-var name carrying HERMES_LLM_API_KEY (ADR-055 §4). NOT
# derived as f"{provider.upper()}_API_KEY" — most names differ (gemini→GOOGLE_API_KEY,
# huggingface→HF_TOKEN, zai→GLM_API_KEY, …). Source: the image cli-config.yaml.example/.env.example.
# A provider absent here falls back to the conservative "<PROVIDER_UPPER>_API_KEY" (see
# hermes_provider_key_env) — only providers with a known non-derivable name are listed.
HERMES_PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "nous-api": "NOUS_API_KEY",
    "nous": "NOUS_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "huggingface": "HF_TOKEN",
    "nvidia": "NVIDIA_API_KEY",
    "lmstudio": "LM_API_KEY",
    # NOTE: `custom` is intentionally NOT here — it has no env-key (env_vars=() in the image); its
    # key is passed via config.yaml model.api_key (ADR-055 §6, HERMES_PROVIDERS_CONFIG_API_KEY).
}

# ADR-055 §6 (closes Q-055-1): providers that take the LLM key via config.yaml ``model.api_key``
# (an env-ref), NOT via a ``<PROVIDER>_API_KEY`` env var. Confirmed from the image: `custom`
# declares env_vars=() and resolves credentials from config.yaml model.api_key only (a passed
# CUSTOM_API_KEY is ignored → upstream 401). `lmstudio` is NOT here — it reads LM_API_KEY. Keep sync
# with the image (Q-055-2). The key value itself is supplied to the container via the env-var named
# below and referenced from config.yaml as "${HERMES_INSTANCE_LLM_KEY}" (never inlined in the file).
HERMES_PROVIDERS_CONFIG_API_KEY: frozenset[str] = frozenset({"custom"})

# Fixed env-var name carrying the LLM key for config-api-key providers (ADR-055 §6). Neutral name
# (does not collide with any real provider key-env); config.yaml references it as an ${...} env-ref.
HERMES_INSTANCE_LLM_KEY_ENV = "HERMES_INSTANCE_LLM_KEY"

# Providers that REQUIRE a model.base_url (ADR-055 §2/§4): provisioning fails fast when
# HERMES_LLM_BASE_URL is empty for one of these. `lmstudio` accepts an optional base_url (the image
# has a default 127.0.0.1:1234/v1) and is therefore NOT required here.
HERMES_PROVIDERS_REQUIRING_BASE_URL: frozenset[str] = frozenset({"custom", "azure-foundry"})


def hermes_provider_key_env(provider: str) -> str:
    """Container env-var name for the instance LLM key, by provider (ADR-055 §4).

    Uses the explicit HERMES_PROVIDER_KEY_ENV map (the image's key-env names are not derivable from
    the provider id). For a provider not in the map, falls back to the conservative
    ``<PROVIDER_UPPER>_API_KEY`` (non-secret derivation; the value is still the same secret key).
    """
    return HERMES_PROVIDER_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
