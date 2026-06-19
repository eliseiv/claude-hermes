"""Application configuration from environment (pydantic-settings).

All secrets and tunables come from env / secret manager (05-security.md, 07-deployment.md).
No magic numbers in business code: limits and grant size are config-driven (ADR-006).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/claude_ios",
        alias="DATABASE_URL",
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

    # --- Observability ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    metrics_scrape_token: str = Field(default="", alias="METRICS_SCRAPE_TOKEN")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
