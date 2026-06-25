"""Subscription: admin subscription-grant (ADR-048/052). StoreKit sync RETIRED (TD-021/ADR-029).

The shared ``StoreKitVerifier`` is re-exported for the consumable token-purchase path (ADR-015),
which still uses it; the subscription ``sync`` path and ``SubscriptionResult`` are removed.
"""

from app.subscription.service import AdminGrantResult, SubscriptionService
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction

__all__ = [
    "AdminGrantResult",
    "SubscriptionService",
    "StoreKitVerifier",
    "VerifiedTransaction",
]
