# --- T-BANK --- module level: """Background worker that polls the card provider for stale 
"""Background worker that polls the card provider for stale pending payments.

Runs every POLL_INTERVAL seconds, picks up pending payments older than
MIN_AGE_SECONDS, checks their status at their provider (YooKassa API or
T-Bank GetState, by ``provider`` of the row — the cover for a lost webhook),
and credits generations if the payment succeeded.
"""
# --- /T-BANK ---

import asyncio
import logging

from app.db.connection import get_pool
from app.db import yookassa_repo
from app.services import yookassa as yookassa_service  # noqa: F401  (kept importable for tests)  # --- T-BANK ---
from app.services import payment_gateway  # --- T-BANK ---
from app.services import max_bot
from app.services.referral_notify import notify_referrer
from app.services.analytics import analytics, analytics_user_id
from app.services.notifications_client import (
    NotificationsClient,
    notify_payment_canceled,
    notify_payment_succeeded,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

POLL_INTERVAL = 120  # seconds between sweeps
MIN_AGE_SECONDS = 120  # only check payments older than this


async def _max_external_id(pool, user_id: int) -> int | None:
    """The MAX numeric user id used as the notifications recipient / chat id."""
    raw = await pool.fetchval(
        "SELECT external_id FROM user_identities WHERE user_id = $1 AND platform = 'max'",
        user_id,
    )
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def poll_pending_payments_loop(
    notifications_client: NotificationsClient | None = None,
    notifications_client_max: NotificationsClient | None = None,
) -> None:
    """Long-running loop — launch as asyncio.create_task() at startup."""
    logger.info("pending_payments worker started (interval=%ss, min_age=%ss)", POLL_INTERVAL, MIN_AGE_SECONDS)
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            await _sweep(notifications_client, notifications_client_max)
        except Exception:
            logger.exception("pending_payments sweep failed")


# --- T-BANK --- changed: _sweep
async def _sweep(
    notifications_client: NotificationsClient | None = None,
    notifications_client_max: NotificationsClient | None = None,
) -> None:
    pool = get_pool()
    pending = await yookassa_repo.get_stale_pending(pool, min_age_seconds=MIN_AGE_SECONDS)
    if not pending:
        return

    logger.info("pending_payments sweep: checking %d stale payment(s)", len(pending))

    for p in pending:
        yookassa_id = p["yookassa_id"]
        try:
            yk_status = await payment_gateway.get_payment_status(p)
            remote_status = yk_status.get("status")

            if remote_status == "succeeded":
                if not payment_gateway.matches_local_payment(yk_status, p):
                    logger.error("pending_payments: provider data mismatch for yookassa_id=%s", yookassa_id)
                    continue
                await yookassa_repo.save_payment_method(pool, yookassa_id, yk_status.get("payment_method"))
                paid = await yookassa_repo.settle_succeeded_payment(pool, yookassa_id)
                if paid:
                    remaining = paid["new_balance"]
                    is_first = paid["is_first_purchase"]
                    telegram_id = paid.get("telegram_id")
                    await notify_referrer(notifications_client, pool, paid.get("referrer_reward"))
                    # The notifications service is Telegram-only. MAX has no
                    # Telegram chat id, yet its payment must still settle in
                    # this recovery worker after a missed provider webhook.
                    if notifications_client is not None and telegram_id is not None:
                        await notifications_client.cancel_event(
                            "onboarding_paywall_completed", telegram_id,
                        )
                        await notifications_client.cancel_event(
                            "first_payment_upsell_cancelled", telegram_id,
                        )
                        await notify_payment_succeeded(
                            notifications_client,
                            telegram_id,
                            paid.get("credited_generations", paid["generations"]),
                            remaining,
                            settings.BOT_USERNAME,
                            settings.API_SECRET_TOKEN,
                            cleanup_message_id=paid.get("payment_message_id"),
                        )
                        if is_first:
                            async with pool.acquire() as conn:
                                first_name = await conn.fetchval(
                                    "SELECT first_name FROM users WHERE telegram_id = $1",
                                    telegram_id,
                                )
                            await notifications_client.fire_event(
                                "first_payment_completed",
                                telegram_id,
                                {"first_name": first_name or ""},
                            )
                    elif str(paid.get("platform") or "").startswith("max"):
                        await max_bot.notify_payment_succeeded(
                            pool,
                            user_id=paid["user_id"],
                            generations=paid.get("credited_generations", paid["generations"]),
                            new_balance=remaining,
                            amount_rubles=paid["amount_rubles"],
                            bonus_generations=paid.get("bonus_generations", 0),
                        )
                        if notifications_client_max is not None:
                            external_id = await _max_external_id(pool, paid["user_id"])
                            if external_id is not None:
                                await notifications_client_max.cancel_event(
                                    "onboarding_paywall_completed", external_id,
                                )
                                await notifications_client_max.cancel_event(
                                    "first_payment_upsell_cancelled", external_id,
                                )
                                if is_first:
                                    async with pool.acquire() as conn:
                                        first_name = await conn.fetchval(
                                            "SELECT first_name FROM users WHERE id = $1",
                                            paid["user_id"],
                                        )
                                    await notifications_client_max.fire_event(
                                        "first_payment_completed",
                                        external_id,
                                        {"first_name": first_name or ""},
                                    )
                    platform = "telegram" if telegram_id is not None else (paid.get("platform") or "max")
                    analytics_uid = (
                        str(telegram_id)
                        if telegram_id is not None
                        else analytics_user_id(platform, paid["user_id"])
                    )
                    await analytics.track(
                        "yookassa_payment_succeeded",
                        user_id=analytics_uid,
                        properties={
                            "yookassa_id": yookassa_id,
                            "amount_rubles": paid["amount_rubles"],
                            "generations": paid["generations"],
                            "new_balance": remaining,
                            "source": "background_poll",
                            "platform": platform,
                            "provider": payment_gateway.provider_of(p),
                        },
                    )
                    logger.info(
                        "pending_payments: credited %d gens to user %s (yookassa_id=%s, balance=%s)",
                        paid["generations"], paid["telegram_id"], yookassa_id, remaining,
                    )

            elif remote_status == "canceled":
                canceled = await yookassa_repo.mark_canceled(pool, yookassa_id)
                if canceled and canceled.get("telegram_id") is not None:
                    await notify_payment_canceled(
                        notifications_client,
                        canceled["telegram_id"],
                        settings.API_SECRET_TOKEN,
                        cleanup_message_id=canceled.get("payment_message_id"),
                    )
                logger.info("pending_payments: canceled yookassa_id=%s", yookassa_id)

            else:
                logger.debug(
                    "pending_payments: yookassa_id=%s still %s", yookassa_id, remote_status,
                )

        except Exception:
            logger.exception("pending_payments: failed to check yookassa_id=%s", yookassa_id)
# --- /T-BANK ---
