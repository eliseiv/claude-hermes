"""Two-step webhook user resolution: deviceId/userId -> our internal userId.

Payment aggregators send ``AccountId`` / ``customer_user_id`` as a **deviceId** (the id the
client passed to the aggregator), not necessarily our internal ``userId``. Both webhooks
previously only checked ``users.id`` and dropped a real payment as ``user_not_found``; the
deviceId -> userId link lives in ``auth_devices`` (ADR-018). Ported from claude-ios ADR-053/055
without the later ``legacy_user_ids`` branch (that table does not exist here).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RESOLVED_VIA_USER_ID = "user_id"
RESOLVED_VIA_DEVICE_ID = "device_id"


async def resolve_user(session: AsyncSession, x: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """Resolve the webhook identifier ``X`` to our ``userId``.

    First-match wins, deterministic (``users`` before ``auth_devices``):

    - (a) ``X`` in ``users`` -> ``X`` already IS our userId; ``resolved_via = "user_id"``.
    - (b) else ``lower(X)`` matches ``lower(auth_devices.device_id)`` -> take the linked
      ``user_id``; ``resolved_via = "device_id"``.
    - (c) else -> ``None`` (=> ``user_not_found``; we never provision users/devices here).

    ``auth_devices.device_id`` is TEXT and may be stored in the client's casing (iOS
    ``identifierForVendor.uuidString`` is UPPERCASE). ``str(x)`` is always lowercase, so
    branch (b) compares case-insensitively via ``lower(device_id)``.
    """
    if await session.scalar(
        text("SELECT 1 FROM users WHERE id = :x"),
        {"x": str(x)},
    ):
        return x, RESOLVED_VIA_USER_ID

    device_user_id = await session.scalar(
        text(
            "SELECT user_id FROM auth_devices WHERE lower(device_id) = :x ORDER BY user_id LIMIT 1"
        ),
        {"x": str(x)},
    )
    if device_user_id is not None:
        return uuid.UUID(str(device_user_id)), RESOLVED_VIA_DEVICE_ID

    return None
