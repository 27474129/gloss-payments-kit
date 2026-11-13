"""Which card provider takes a payment, and one door to both of them.

The payments table, the settlement and every side effect after it are shared (``yookassa_payments``,
``yookassa_repo.settle_succeeded_payment``); only the three provider calls differ: create, read the status,
compare it with the stored intent. Call sites pick the provider with ``choose_provider`` when a payment is
created and afterwards follow ``payment["provider"]`` stored in the row.

In test mode (``TBANK_TEST_MODE``, preview) only ``TBANK_TEST_USER_IDS`` may pay, through T-Bank only; others get 403.

Choice (``tbank_acq.is_configured()`` is required for any T-Bank traffic, so empty keys = YooKassa only):
1. ``TBANK_FORCE_USER_IDS`` (users.id) — always T-Bank (owner smoke test);
2. ``PAYMENT_PROVIDER=tbank`` — everybody;
3. ``TBANK_ROLLOUT_PERCENT`` — users whose stable bucket (hash of users.id, 0..99) is below the percent;
   raising the percent only adds users, nobody flips back and forth;
4. otherwise YooKassa.
"""

import hashlib
import logging

from fastapi import HTTPException

from app.core.config import settings
from app.services import tbank_acq
from app.services import yookassa as yookassa_service

logger = logging.getLogger(__name__)

YOOKASSA = "yookassa"
TBANK = "tbank"


def bucket(user_id: int) -> int:
    """Stable 0..99 position of a user for the percentage rollout."""
    digest = hashlib.sha256(f"tbank-rollout:{int(user_id)}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def choose_provider(user_id: int) -> str:
    if settings.TBANK_TEST_MODE:
        # Test terminal: only the owner's accounts pay, always through T-Bank (no real money, no YooKassa).
        if int(user_id) not in settings.tbank_test_user_ids or not tbank_acq.is_configured():
            raise HTTPException(status_code=403, detail="T-Bank test mode: only test accounts can pay")
        return TBANK
    if not tbank_acq.is_configured():
        return YOOKASSA
    if int(user_id) in settings.tbank_force_user_ids:
        return TBANK
    if (settings.PAYMENT_PROVIDER or "").strip().lower() == TBANK:
        return TBANK
    percent = max(0, min(100, int(settings.TBANK_ROLLOUT_PERCENT or 0)))
    if percent and bucket(user_id) < percent:
        return TBANK
    return YOOKASSA


def yookassa_configured() -> bool:
    return bool(settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY)


def configured(provider: str) -> bool:
    return tbank_acq.is_configured() if provider == TBANK else yookassa_configured()


def provider_of(payment: dict | None) -> str:
    """The provider a stored payment row belongs to (rows from before the T-Bank migration have no key)."""
    return TBANK if (payment or {}).get("provider") == TBANK else YOOKASSA


async def create_payment(provider: str, **kwargs) -> dict:
    """Create the payment at ``provider``; the result has ``yookassa_id``, ``confirmation_url``, ``provider``
    (the one that actually took it) and, for T-Bank, ``order_id``.

    ``kwargs`` are those of ``yookassa.create_sbp_payment``. The T-Bank payment is always a redirect to its
    own form (``payment_method``/``confirmation_type``/``locale`` are YooKassa-only and ignored). There is no automatic
    fallback: a T-Bank failure at checkout raises ``TBankError`` and the buyer sees an error (watch the logs).
    """
    fail_url = kwargs.pop("fail_url", None)
    device_data = kwargs.pop("device_data", None)  # T-Bank only: ``DATA`` flags of a wallet
    if provider == TBANK:
        return await tbank_acq.create_payment(
            kwargs["amount_rubles"], kwargs["description"], kwargs["generations"],
            user_id=kwargs["user_id"], telegram_id=kwargs.get("telegram_id"),
            return_url=kwargs.get("return_url"), fail_url=fail_url, extra_metadata=kwargs.get("extra_metadata"),
            **({"device_data": device_data} if device_data else {}),
        )
    result = await yookassa_service.create_sbp_payment(**kwargs)
    return {**result, "provider": YOOKASSA, "order_id": None}


async def get_payment_status(payment: dict) -> dict:
    """The provider's view of a stored payment row, in the YooKassa-shaped dict (``status`` succeeded|canceled|pending)."""
    if provider_of(payment) == TBANK:
        return await tbank_acq.get_payment_status(payment["yookassa_id"])
    return await yookassa_service.get_payment_status(payment["yookassa_id"])


def matches_local_payment(remote: dict, local: dict) -> bool:
    if provider_of(local) == TBANK:
        return tbank_acq.matches_local_payment(remote, local)
    return yookassa_service.matches_local_payment(remote, local)
