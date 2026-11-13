"""Database operations for yookassa_payments table."""

import logging

import asyncpg
from app.db import metrika_purchase_repo, referrals_repo, vk_pixel_repo
from app.services import first_purchase_bonus

logger = logging.getLogger(__name__)


# --- T-BANK --- changed: create_payment
async def create_payment(
    pool: asyncpg.Pool,
    user_id: int,
    telegram_id: int | None,
    yookassa_id: str,
    amount_rubles: int,
    generations: int,
    confirmation_url: str | None,
    *,
    platform: str = "telegram",
    operation_key: str | None = None,
    offer_slug: str | None = None,
    bonus_generations: int = 0,
    provider: str = "yookassa",
    provider_order_id: str | None = None,
) -> dict:
    """Store a pending payment. ``yookassa_id`` is the provider's payment id for either provider
    (T-Bank ``PaymentId`` when ``provider='tbank'``, with its ``OrderId`` in ``provider_order_id``)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO yookassa_payments
                (user_id, telegram_id, yookassa_id, amount_rubles, generations, status, confirmation_url, platform,
                 client_operation_key, offer_slug, bonus_generations, provider, provider_order_id)
            VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10, $11, $12)
            RETURNING id, yookassa_id, status, confirmation_url, created_at
            """,
            user_id, telegram_id, yookassa_id, amount_rubles, generations, confirmation_url, platform,
            operation_key, offer_slug, bonus_generations, provider, provider_order_id,
        )
        return dict(row)
# --- /T-BANK ---


async def get_by_operation_key(
    pool: asyncpg.Pool, *, user_id: int, platform: str, operation_key: str,
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, yookassa_id, status, confirmation_url, amount_rubles, generations, bonus_generations
            FROM yookassa_payments
            WHERE user_id = $1 AND platform = $2 AND client_operation_key = $3
            """,
            user_id, platform, operation_key,
        )
        return dict(row) if row else None


# --- T-BANK --- changed: create_payment_on_connection
async def create_payment_on_connection(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    telegram_id: int | None,
    yookassa_id: str,
    amount_rubles: int,
    generations: int,
    confirmation_url: str | None,
    platform: str,
    operation_key: str,
    offer_slug: str | None = None,
    bonus_generations: int = 0,
    provider: str = "yookassa",
    provider_order_id: str | None = None,
) -> dict:
    row = await conn.fetchrow(
        """
        INSERT INTO yookassa_payments
            (user_id, telegram_id, yookassa_id, amount_rubles, generations, status, confirmation_url, platform,
             client_operation_key, offer_slug, bonus_generations, provider, provider_order_id)
        VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10, $11, $12)
        RETURNING id, yookassa_id, status, confirmation_url, created_at
        """,
        user_id, telegram_id, yookassa_id, amount_rubles, generations,
        confirmation_url, platform, operation_key, offer_slug, bonus_generations, provider, provider_order_id,
    )
    return dict(row)
# --- /T-BANK ---


async def mark_succeeded(pool: asyncpg.Pool, yookassa_id: str) -> dict | None:
    """Mark payment as succeeded. Returns payment row or None if not found."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE yookassa_payments
            SET status = 'succeeded', completed_at = NOW()
            WHERE yookassa_id = $1 AND status = 'pending'
            RETURNING id, user_id, telegram_id, yookassa_id, amount_rubles, generations
            """,
            yookassa_id,
        )
        return dict(row) if row else None


# --- T-BANK --- changed: settle_succeeded_payment
async def settle_succeeded_payment(pool: asyncpg.Pool, yookassa_id: str) -> dict | None:
    """Atomically mark a payment, credit balance, log it, and mark purchase.

    A payment of the T-Bank TEST terminal (``provider_order_id`` starts with ``gltest``) is credited the same way but
    stored as ``test_owner`` — the status every report, the NPD receipts and the ad guard already leave out — and
    touches nothing that counts money or marketing: no ``has_purchased``, no referral reward, no Metrika/VK
    conversion. The returned row has ``test=True``.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            payment = await conn.fetchrow(
                """
                UPDATE yookassa_payments
                SET status = CASE WHEN provider = 'tbank' AND provider_order_id LIKE 'gltest%' THEN 'test_owner' ELSE 'succeeded' END,
                    completed_at = NOW()
                WHERE yookassa_id = $1 AND status = 'pending'
                RETURNING id, user_id, telegram_id, platform, yookassa_id, amount_rubles, generations, payment_message_id,
                          offer_slug, bonus_generations AS menu_bonus_generations, provider,
                          (status = 'test_owner') AS test
                """,
                yookassa_id,
            )
            if payment is None:
                return None

            is_test = bool(payment["test"])
            first_purchase = None if is_test else await conn.fetchval(
                """
                UPDATE users
                SET has_purchased = TRUE, updated_at = NOW()
                WHERE id = $1 AND has_purchased = FALSE
                RETURNING TRUE
                """,
                payment["user_id"],
            )
            # The menu bonus was fixed at checkout and rides only on menu payments; the first-purchase
            # bonus rides only on the ``*_bonus`` platforms, so the two never add up.
            bonus_generations = int(payment["menu_bonus_generations"] or 0) + first_purchase_bonus.settlement_bonus(
                platform=payment["platform"],
                generations=payment["generations"],
                first_purchase=bool(first_purchase),
            )
            if is_test:
                referral = {"friend_bonus": 0, "referrer_reward": None}
            else:
                referral = await referrals_repo.apply_settlement(
                    conn, user_id=payment["user_id"], provider="yookassa", payment_id=payment["id"],
                    amount_rubles=payment["amount_rubles"], generations=payment["generations"],
                )
            bonus_generations += referral["friend_bonus"]
            credited_generations = payment["generations"] + bonus_generations
            if not is_test:
                await metrika_purchase_repo.enqueue_first_purchase(
                    conn, user_id=payment["user_id"], provider="yookassa", provider_payment_id=payment["yookassa_id"],
                    amount_rubles=payment["amount_rubles"], is_first_purchase=bool(first_purchase),
                )
            if first_purchase:
                await vk_pixel_repo.enqueue(
                    conn, user_id=payment["user_id"], event=vk_pixel_repo.PURCHASE, provider="yookassa",
                    provider_payment_id=payment["yookassa_id"], amount_rubles=payment["amount_rubles"],
                )

            balance = await conn.fetchrow(
                """
                UPDATE balances
                SET generations_remaining = generations_remaining + $1,
                    updated_at = NOW()
                WHERE user_id = $2
                RETURNING generations_remaining
                """,
                credited_generations, payment["user_id"],
            )
            if balance is None:
                raise RuntimeError(f"Balance not found for payment {yookassa_id}")

            label = "T-Bank TEST" if is_test else "T-Bank" if payment["provider"] == "tbank" else "YooKassa SBP"
            await conn.execute(
                """
                INSERT INTO payments (user_id, generations_credited, comment)
                VALUES ($1, $2, $3)
                """,
                payment["user_id"], credited_generations,
                (
                    f"{label}: {payment['amount_rubles']}₽"
                    f" + {bonus_generations} бонусных"
                    if bonus_generations
                    else f"{label}: {payment['amount_rubles']}₽"
                ),
            )

            result = dict(payment)
            result.pop("menu_bonus_generations", None)
            result["new_balance"] = balance["generations_remaining"]
            result["is_first_purchase"] = bool(first_purchase)
            result["bonus_generations"] = bonus_generations
            result["credited_generations"] = credited_generations
            result["referrer_reward"] = referral["referrer_reward"]
            return result
# --- /T-BANK ---


# --- T-BANK --- changed: save_payment_method
async def save_payment_method(pool: asyncpg.Pool, yookassa_id: str, method: dict | None) -> None:
    """Remember how a payment was paid (card first6/last4 and type), for abuse monitoring and the ways-to-pay split.

    Every column is only filled while it is empty, so the first answer wins and a later one completes it: T-Bank's
    notification brings the card (no type), its GetState brings the type (no card), and they may come in either order.
    Best effort: it never raises, so a failure here cannot hold back the settlement.
    """
    if not method or not (method.get("type") or method.get("card_last4")):
        return
    try:
        await pool.execute(
            """
            UPDATE yookassa_payments
               SET payment_method_type = COALESCE(payment_method_type, $2),
                   card_first6 = COALESCE(card_first6, $3), card_last4 = COALESCE(card_last4, $4),
                   card_type = COALESCE(card_type, $5), card_country = COALESCE(card_country, $6)
             WHERE yookassa_id = $1
               AND (payment_method_type IS NULL OR (card_last4 IS NULL AND $4::text IS NOT NULL))
            """,
            yookassa_id, method.get("type"), method.get("card_first6"), method.get("card_last4"),
            method.get("card_type"), method.get("card_country"),
        )
    except Exception:
        logger.warning("could not save the payment method of %s", yookassa_id, exc_info=True)
# --- /T-BANK ---


async def set_payment_message_id(
    pool: asyncpg.Pool,
    telegram_id: int,
    yookassa_id: str,
    payment_message_id: int,
) -> dict | None:
    """Store the bot message that should disappear once this payment succeeds."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE yookassa_payments
            SET payment_message_id = $3
            WHERE telegram_id = $1 AND yookassa_id = $2
            RETURNING status, payment_message_id
            """,
            telegram_id, yookassa_id, payment_message_id,
        )
        return dict(row) if row else None


async def mark_canceled(pool: asyncpg.Pool, yookassa_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE yookassa_payments
            SET status = 'canceled', completed_at = NOW()
            WHERE yookassa_id = $1 AND status = 'pending'
            RETURNING id, telegram_id, yookassa_id, amount_rubles, generations, payment_message_id
            """,
            yookassa_id,
        )
        return dict(row) if row else None


# --- T-BANK --- new: reopen_canceled
async def reopen_canceled(pool: asyncpg.Pool, yookassa_id: str) -> bool:
    """A T-Bank payment we closed as failed was paid after all (a retry on the same form): make it pending again.

    Only for ``provider='tbank'``; the caller has verified the signature/amount of the CONFIRMED notification or
    GetState. YooKassa keeps its own rule (a canceled payment stays canceled).
    """
    reopened = await pool.fetchval(
        """
        UPDATE yookassa_payments SET status = 'pending', completed_at = NULL
        WHERE yookassa_id = $1 AND provider = 'tbank' AND status = 'canceled'
        RETURNING TRUE
        """,
        yookassa_id,
    )
    return bool(reopened)
# --- /T-BANK ---


# --- T-BANK --- new: record_refund
async def record_refund(pool: asyncpg.Pool, yookassa_id: str, refunded_rubles) -> dict | None:
    """Remember how much of a payment went back to the buyer (never decreases). The row stays ``succeeded``,
    like a refunded YooKassa payment; the NPD sync reads ``refunded_rubles``."""
    row = await pool.fetchrow(
        """
        UPDATE yookassa_payments SET refunded_rubles = GREATEST(refunded_rubles, LEAST($2::numeric, amount_rubles))
        WHERE yookassa_id = $1
        RETURNING id, yookassa_id, user_id, amount_rubles, refunded_rubles, provider
        """,
        yookassa_id, refunded_rubles,
    )
    return dict(row) if row else None
# --- /T-BANK ---


async def get_by_yookassa_id(pool: asyncpg.Pool, yookassa_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM yookassa_payments WHERE yookassa_id = $1",
            yookassa_id,
        )
        return dict(row) if row else None


# --- T-BANK --- new: list_by_operation_prefix
async def list_by_operation_prefix(pool: asyncpg.Pool, *, user_id: int, prefix: str) -> list[dict]:
    """The payments one Mini App ticket started (operation keys ``tk:<nonce>:<way>``), the newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM yookassa_payments
            WHERE user_id = $1 AND client_operation_key LIKE $2
            ORDER BY created_at DESC, id DESC
            """,
            user_id, prefix + "%",
        )
        return [dict(row) for row in rows]
# --- /T-BANK ---


async def get_pending_by_telegram_id(pool: asyncpg.Pool, telegram_id: int) -> dict | None:
    """Get the most recent pending payment for a user."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM yookassa_payments
            WHERE telegram_id = $1 AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            telegram_id,
        )
        return dict(row) if row else None


async def get_all_pending_by_telegram_id(pool: asyncpg.Pool, telegram_id: int) -> list[dict]:
    """Get ALL pending payments for a user, oldest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM yookassa_payments
            WHERE telegram_id = $1 AND status = 'pending'
            ORDER BY created_at ASC
            """,
            telegram_id,
        )
        return [dict(r) for r in rows]


async def get_all_pending_by_user_id(pool: asyncpg.Pool, user_id: int) -> list[dict]:
    """Get ALL pending payments for a user, oldest first — platform-agnostic."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM yookassa_payments
            WHERE user_id = $1 AND status = 'pending'
            ORDER BY created_at ASC
            """,
            user_id,
        )
        return [dict(r) for r in rows]


async def get_pending_by_user_id_and_yookassa_id(
    pool: asyncpg.Pool, user_id: int, yookassa_id: str
) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM yookassa_payments WHERE user_id = $1 AND yookassa_id = $2 AND status = 'pending'",
            user_id, yookassa_id,
        )
        return dict(row) if row else None


async def get_stale_pending(pool: asyncpg.Pool, min_age_seconds: int = 120, limit: int = 50) -> list[dict]:
    """Get pending payments older than min_age_seconds, oldest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM yookassa_payments
            WHERE status = 'pending'
              AND created_at < NOW() - make_interval(secs => $1::float)
            ORDER BY created_at ASC
            LIMIT $2
            """,
            float(min_age_seconds), limit,
        )
        return [dict(r) for r in rows]
