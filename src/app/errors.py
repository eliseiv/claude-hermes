"""Domain technical errors mapped to HTTP codes (api-gateway/02-api-contracts.md, ADR-004).

Business blocks are NOT errors — they return 200 {status: blocked} (ADR-004).
These exceptions cover only technical failures (4xx/5xx).
"""

from __future__ import annotations


class AppError(Exception):
    """Base technical error. `code` is from the standard error enum."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.code
        super().__init__(self.message)


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class SubscriptionRequiredError(ForbiddenError):
    """Token purchase attempted without an active subscription (Q-015-1=B, ADR-015).

    403 with code=subscription_required: the value is reused from the ADR-004 enum but emitted
    as a 4xx error code (not a 200 blockReason) — token purchase is a top-up operation, not
    generation, so ADR-004 (blocked = 200) does not apply.
    """

    code = "subscription_required"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class SessionNotFoundError(NotFoundError):
    """sessionId passed to /wallet/consume does not exist in chat_sessions (wallet-ledger/02)."""

    code = "session_not_found"


class UserNotFoundError(NotFoundError):
    """userId targeted by an admin op does not exist; admin never creates users (ADR-009)."""

    code = "user_not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InsufficientCreditsError(ConflictError):
    """Balance changed below required amount after policy allow (wallet-ledger/02)."""

    code = "insufficient_credits"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_error"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


class ServiceUnavailableError(AppError):
    """A required dependency/feature is not configured (e.g. auth issuer has no private key).

    503 service_unavailable: used by the embedded auth-issuer endpoints when no private signing
    key is configured (ADR-018 §7); verify-only mode keeps working on the public key.
    """

    status_code = 503
    code = "service_unavailable"
