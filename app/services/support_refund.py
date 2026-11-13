# --- T-BANK --- module level: """Refunds of the support inbox: the calculation by the offe
"""Refunds of the support inbox: the calculation by the offer and the provider call (YooKassa refund,
or T-Bank ``Cancel`` for payments with ``provider = 'tbank'``).

The model only *calculates* (``plan_refunds``) and proposes; money moves exclusively from the operator's
button (``routes/inbox.py`` → ``execute_refund``). The rule is clause 6.2 of the offer: for the unused part of a
package the customer gets ``price × unused ⚡ / ⚡ in the package``; the commission of the payment service is
NOT kept (``SUPPORT_REFUND_DEDUCT_FEE=1`` switches the deduction on — the share of the actual YooKassa
commission, ``amount − income_amount``, that falls on the refunded part, or 3.5 % when YooKassa did not report it).

Which ⚡ are "unused" from which package: spending is assumed to take the oldest energy first, so the
balance that is left belongs to the newest packages (FIFO).
"""
# --- /T-BANK ---
from __future__ import annotations

import json
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg
import httpx

from app.core.config import settings
from app.services import tbank_acq  # --- T-BANK ---

logger = logging.getLogger(__name__)

KOPECK = Decimal("0.01")


class RefundError(Exception):
    """A refund cannot be made or the provider refused it. ``unknown`` = the outcome at YooKassa is unknown."""

    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(KOPECK, rounding=ROUND_HALF_UP)


def allocate_unused(packages: list[dict], balance: int, free_credits: list[dict] | None = None) -> list[dict]:
    """Spread the energy that is left over the credits, newest first (the oldest ⚡ were spent first).

    ``packages`` — succeeded payments in any order, each with ``generations``; ``free_credits`` — ⚡ the support gave
    for nothing (compensations, bonuses, manual grants: ``created_at``, ``amount``): when they are newer than a
    package they are what is left first and are never paid back in money. Returns the packages newest first with
    ``unused`` ⚡ added (0 for fully spent ones).
    """
    entries = [("package", p["created_at"], p["id"], int(p["generations"]), p) for p in packages]
    entries += [("free", c["created_at"], 0, int(c["amount"]), c) for c in free_credits or []]
    left = max(0, int(balance))
    unused: dict[int, int] = {}
    for kind, _created, ident, amount, _item in sorted(entries, key=lambda e: (e[1], e[2]), reverse=True):
        take = min(amount, left)
        left -= take
        if kind == "package":
            unused[ident] = take
    return [{**p, "unused": unused[p["id"]]} for p in sorted(packages, key=lambda p: (p["created_at"], p["id"]), reverse=True)]


def calc_refund(
    *, price: Any, package: int, unused: int, income_amount: Any | None = None,
    deduct_fee: bool = False, fallback_fee_percent: float = 3.5,
) -> dict:
    """The refund for ``unused`` ⚡ of a ``package``-⚡ payment of ``price`` ₽ (all values as the card shows them)."""
    price_d = money(price)
    package = int(package)
    unused = max(0, min(int(unused), package))
    if package <= 0 or unused <= 0:
        return {"price": str(price_d), "package": package, "unused": 0, "gross": "0.00", "fee": "0.00",
                "fee_deducted": False, "amount": "0.00", "fee_source": None}
    gross = money(price_d * unused / package)
    if income_amount is not None:
        fee_total = max(Decimal("0"), price_d - money(income_amount))
        fee_source = "income_amount"
    else:
        fee_total = money(price_d * Decimal(str(fallback_fee_percent)) / 100)
        fee_source = "fallback"
    fee_part = money(fee_total * unused / package)
    amount = max(Decimal("0"), gross - fee_part) if deduct_fee else gross
    return {
        "price": str(price_d), "package": package, "unused": unused, "gross": str(gross),
        "fee": str(fee_part), "fee_total": str(fee_total), "fee_source": fee_source,
        "fee_deducted": bool(deduct_fee), "amount": str(amount),
    }


# --- YooKassa API ----------------------------------------------------------------------------------

def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.YOOKASSA_API_BASE.rstrip("/"), timeout=httpx.Timeout(30.0, connect=10.0),
        auth=(str(settings.YOOKASSA_SHOP_ID), settings.YOOKASSA_SECRET_KEY), transport=transport,
    )


def _check_configured() -> None:
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        raise RefundError("ЮKassa не настроена")


async def fetch_payment(yookassa_id: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    _check_configured()
    try:
        async with _client(transport) as http:
            response = await http.get(f"/payments/{yookassa_id}")
    except httpx.HTTPError as exc:
        raise RefundError("ЮKassa не ответила, попробуйте ещё раз") from exc
    if response.status_code != 200:
        raise RefundError(f"ЮKassa вернула {response.status_code} при чтении платежа")
    return response.json()


def income_of(payment: dict) -> Decimal | None:
    value = (payment.get("income_amount") or {}).get("value")
    return money(value) if value is not None else None


def refundable_left(payment: dict) -> Decimal:
    """What YooKassa still lets us return of this payment."""
    total = money((payment.get("amount") or {}).get("value", 0))
    refunded = money((payment.get("refunded_amount") or {}).get("value", 0))
    return total - refunded


async def create_refund(
    yookassa_id: str, amount: Decimal, *, idempotence_key: str, description: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST /v3/refunds. The same ``idempotence_key`` makes a retry return the same refund, never a second one."""
    _check_configured()
    payload = {
        "payment_id": yookassa_id, "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "description": description[:250],
    }
    try:
        async with _client(transport) as http:
            response = await http.post("/refunds", json=payload, headers={"Idempotence-Key": idempotence_key})
    except httpx.HTTPError as exc:
        raise RefundError("ЮKassa не ответила — результат неизвестен, повторите кнопкой (повтор безопасен)", unknown=True) from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise RefundError(f"ЮKassa временно недоступна ({response.status_code}) — повторите кнопкой", unknown=True)
    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = f"{body.get('code', '')}: {body.get('description', '')}"
        except ValueError:
            detail = response.text[:200]
        raise RefundError(f"ЮKassa отказала ({response.status_code}) {detail}".strip())
    return response.json()


async def fetch_refund(refund_id: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    _check_configured()
    try:
        async with _client(transport) as http:
            response = await http.get(f"/refunds/{refund_id}")
    except httpx.HTTPError as exc:
        raise RefundError("ЮKassa не ответила", unknown=True) from exc
    if response.status_code != 200:
        raise RefundError(f"ЮKassa вернула {response.status_code} при чтении возврата")
    return response.json()


# --- T-Bank (payments with provider = 'tbank') ----------------------------------------------------

# --- T-BANK --- new: tbank_refund_id
def tbank_refund_id(payment_id: str) -> str:
    """``support_refunds.refund_id`` of a T-Bank refund (Cancel gives no refund id of its own)."""
    return f"tbank:{payment_id}"
# --- /T-BANK ---


# --- T-BANK --- new: tbank_cancel
async def tbank_cancel(
    pool: asyncpg.Pool, *, payment: dict, amount: Decimal, retry_after_unknown: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Return ``amount`` of a T-Bank payment (Cancel) and remember it in ``refunded_rubles``.

    Cancel has no idempotency key, so after a lost answer (``retry_after_unknown``) the payment is first
    read back: a refunded status with nothing recorded in ``refunded_rubles`` means that our call went through.
    """
    from app.db import yookassa_repo

    payment_id = payment["yookassa_id"]
    if retry_after_unknown and not Decimal(str(payment.get("refunded_rubles") or 0)):
        try:
            state = await tbank_acq.get_payment_status(payment_id, transport=transport)
        except tbank_acq.TBankError as exc:
            raise RefundError(f"Т-Банк не ответил — результат неизвестен, повторите кнопкой: {exc}", unknown=True) from exc
        if state.get("tbank_status") in tbank_acq.REFUND_STATUSES:
            await yookassa_repo.record_refund(pool, payment_id, amount)
            return {"id": tbank_refund_id(payment_id), "status": "succeeded", "already": True}
    try:
        result = await tbank_acq.cancel_payment(payment_id, amount, transport=transport)
    except tbank_acq.TBankError as exc:
        if exc.unknown:
            raise RefundError(f"Т-Банк не ответил — результат неизвестен, повторите кнопкой: {exc}", unknown=True) from exc
        raise RefundError(f"Т-Банк отказал в возврате: {exc}") from exc
    original, left = result["original_amount"], result["new_amount"]
    refunded = (original - left) if original is not None and left is not None else amount
    await yookassa_repo.record_refund(pool, payment_id, refunded)
    return {"id": tbank_refund_id(payment_id), "status": "succeeded", "tbank_status": result["status"]}
# --- /T-BANK ---


# --- planning (read only) --------------------------------------------------------------------------

# --- T-BANK --- changed: plan_refunds
async def plan_refunds(
    pool: asyncpg.Pool, user_id: int, *, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """What the customer would get back by the offer, per payment. Reads the DB and YooKassa; changes nothing."""
    from app.db import support_energy_repo

    payments = [dict(row) for row in await pool.fetch(
        """
        SELECT p.id, p.yookassa_id, p.created_at, p.amount_rubles,
               p.generations + p.bonus_generations AS generations, p.platform, p.provider, p.refunded_rubles
        FROM yookassa_payments p
        WHERE p.user_id = $1 AND p.status = 'succeeded' AND p.amount_rubles > 0
          AND NOT EXISTS (SELECT 1 FROM support_refunds r WHERE r.payment_id = p.id AND r.status IN ('pending', 'succeeded'))
        """,
        user_id,
    )]
    summary = await support_energy_repo.summary(pool, user_id)
    free_credits = [dict(row) for row in await pool.fetch(
        """
        SELECT created_at, (details->>'amount')::int AS amount FROM support_actions
        WHERE action IN ('grant', 'auto_grant', 'compensate', 'survey_reward') AND (details->>'user_id')::bigint = $1
        """,
        user_id,
    )]
    plan: list[dict] = []
    for item in allocate_unused(payments, summary["balance"], free_credits):
        entry = {
            "payment_id": item["id"], "yookassa_id": item["yookassa_id"], "created_at": item["created_at"],
            "price": item["amount_rubles"], "package": item["generations"], "unused": item["unused"],
        }
        if item["unused"] <= 0:
            entry.update(refundable=False, amount="0.00", note="пакет потрачен полностью")
            plan.append(entry)
            continue
        income = None
        left = None
        if item.get("provider") == "tbank":
            # T-Bank reports no commission per payment (fallback percent applies only if the deduction is on);
            # what can still be returned is the amount minus what we recorded as refunded.
            left = money(item["amount_rubles"]) - money(item["refunded_rubles"] or 0)
        else:
            try:
                remote = await fetch_payment(item["yookassa_id"], transport=transport)
                income = income_of(remote)
                left = refundable_left(remote)
            except RefundError as exc:
                entry["warning"] = str(exc)
        calc = calc_refund(
            price=item["amount_rubles"], package=item["generations"], unused=item["unused"], income_amount=income,
            deduct_fee=settings.SUPPORT_REFUND_DEDUCT_FEE, fallback_fee_percent=settings.SUPPORT_REFUND_FEE_FALLBACK_PERCENT,
        )
        entry.update(calc)
        amount = Decimal(calc["amount"])
        if left is not None and amount > left:
            who = "Т-Банк" if item.get("provider") == "tbank" else "ЮKassa"
            entry.update(refundable=False, note=f"{who} позволяет вернуть только {left} ₽ (уже возвращено)")
        else:
            entry.update(refundable=amount > 0)
        plan.append(entry)
    refundable = [e for e in plan if e.get("refundable")]
    return {
        "summary": summary, "payments": plan,
        "total": str(sum((Decimal(e["amount"]) for e in refundable), Decimal("0"))),
        "total_energy": sum(e["unused"] for e in refundable),
    }
# --- /T-BANK ---


# --- proposals and execution ------------------------------------------------------------------------

async def propose(pool: asyncpg.Pool, *, ticket_id: int | None, user_id: int, plan: dict) -> list[dict]:
    """Store the refundable part of a plan as proposals for the operator; money is not touched."""
    stored = []
    for entry in plan["payments"]:
        if not entry.get("refundable"):
            continue
        row = await pool.fetchrow(
            """
            INSERT INTO support_refunds (ticket_id, user_id, payment_id, yookassa_id, amount_rub, energy, calc, idempotence_key)
            SELECT $1, $2, $3, $4, $5, $6, $7::jsonb, $8
            WHERE NOT EXISTS (SELECT 1 FROM support_refunds WHERE payment_id = $3 AND status IN ('proposed', 'pending', 'succeeded'))
            RETURNING *
            """,
            ticket_id, user_id, entry["payment_id"], entry["yookassa_id"], Decimal(entry["amount"]), entry["unused"],
            json.dumps({k: v for k, v in entry.items() if k != "created_at"}, ensure_ascii=False, default=str),
            uuid.uuid4().hex,
        )
        if row is not None:
            stored.append(dict(row))
    return stored


async def list_refunds(pool: asyncpg.Pool, ticket_id: int) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM support_refunds WHERE ticket_id = $1 ORDER BY id", ticket_id)
    result = []
    for row in rows:
        item = dict(row)
        item["amount_rub"] = str(item["amount_rub"])
        if isinstance(item["calc"], str):
            item["calc"] = json.loads(item["calc"])
        result.append(item)
    return result


# --- T-BANK --- changed: execute_refund
async def execute_refund(
    pool: asyncpg.Pool, *, refund_row_id: int, admin_id: int, expected_amount: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """The operator confirmed: recompute, call YooKassa once (idempotent), take the returned ⚡ off the balance."""
    row = await pool.fetchrow("SELECT * FROM support_refunds WHERE id = $1", refund_row_id)
    if row is None:
        raise RefundError("Предложение о возврате не найдено")
    if row["status"] == "succeeded":
        raise RefundError("Возврат уже выполнен")
    if row["status"] == "dismissed":
        raise RefundError("Предложение отклонено")
    if row["status"] not in ("proposed", "pending", "failed"):
        raise RefundError("Возврат в неподходящем статусе")

    refund_id = row["refund_id"]
    if refund_id is None:
        # Recompute against the live balance and payment: the customer may have spent energy since the proposal.
        plan = await plan_refunds(pool, row["user_id"], transport=transport)
        entry = next((e for e in plan["payments"] if e["payment_id"] == row["payment_id"]), None)
        if row["status"] == "pending":
            # A retry after an unknown outcome: the payment is excluded from plans by the live refund; reuse the stored numbers.
            entry = {"refundable": True, "amount": str(row["amount_rub"]), "unused": row["energy"]}
        if entry is None or not entry.get("refundable"):
            raise RefundError("По этому платежу возвращать уже нечего — пересчитайте")
        if money(entry["amount"]) != money(expected_amount) or money(entry["amount"]) != money(row["amount_rub"]):
            raise RefundError(f"Расчёт изменился (теперь {entry['amount']} ₽) — обновите карточку и подтвердите заново")
        claimed = await pool.fetchval(
            """
            UPDATE support_refunds SET status = 'pending', admin_id = $2, updated_at = NOW(), error = NULL
            WHERE id = $1 AND status IN ('proposed', 'failed', 'pending') RETURNING id
            """,
            refund_row_id, admin_id,
        )
        if claimed is None:
            raise RefundError("Возврат уже выполняется")
        payment = await pool.fetchrow(
            "SELECT yookassa_id, provider, amount_rubles, refunded_rubles FROM yookassa_payments WHERE id = $1",
            row["payment_id"],
        )
        try:
            if payment is not None and payment["provider"] == "tbank":
                provider = await tbank_cancel(
                    pool, payment=dict(payment), amount=money(row["amount_rub"]),
                    retry_after_unknown=row["status"] == "pending", transport=transport,
                )
            else:
                provider = await create_refund(
                    row["yookassa_id"], money(row["amount_rub"]), idempotence_key=row["idempotence_key"],
                    description="Возврат за неиспользованную часть пакета (п. 6.2 оферты)",
                    transport=transport,
                )
        except RefundError as exc:
            if exc.unknown:
                await pool.execute("UPDATE support_refunds SET error = $2, updated_at = NOW() WHERE id = $1", refund_row_id, str(exc)[:500])
            else:
                await pool.execute(
                    "UPDATE support_refunds SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1",
                    refund_row_id, str(exc)[:500],
                )
            raise
        refund_id = provider.get("id")
        provider_status = provider.get("status")
        if provider_status == "canceled":
            reason = (provider.get("cancellation_details") or {}).get("reason", "canceled")
            await pool.execute(
                "UPDATE support_refunds SET status = 'failed', refund_id = $2, error = $3, updated_at = NOW() WHERE id = $1",
                refund_row_id, refund_id, f"ЮKassa отменила возврат: {reason}",
            )
            raise RefundError(f"ЮKassa отменила возврат: {reason}")
        await pool.execute(
            "UPDATE support_refunds SET refund_id = $2, updated_at = NOW() WHERE id = $1", refund_row_id, refund_id,
        )
    elif str(refund_id).startswith("tbank:"):
        provider_status = "succeeded"  # T-Bank Cancel answered earlier; only the balance side is left
    else:
        provider = await fetch_refund(refund_id, transport=transport)
        provider_status = provider.get("status")
        if provider_status == "canceled":
            await pool.execute(
                "UPDATE support_refunds SET status = 'failed', error = 'ЮKassa отменила возврат', updated_at = NOW() WHERE id = $1",
                refund_row_id,
            )
            raise RefundError("ЮKassa отменила возврат")

    return await _settle(pool, refund_row_id=refund_row_id, admin_id=admin_id, provider_status=provider_status)
# --- /T-BANK ---


async def _settle(pool: asyncpg.Pool, *, refund_row_id: int, admin_id: int, provider_status: str | None) -> dict:
    """The money is on its way: take the returned ⚡ off the balance and write the journal, once."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM support_refunds WHERE id = $1 FOR UPDATE", refund_row_id)
        if row["status"] == "succeeded":
            return {"already": True, "refund": dict(row)}
        balance = await conn.fetchval(
            "SELECT generations_remaining FROM balances WHERE user_id = $1 FOR UPDATE", row["user_id"],
        ) or 0
        taken = min(int(balance), int(row["energy"]))
        if taken:
            await conn.execute(
                "UPDATE balances SET generations_remaining = generations_remaining - $2, updated_at = NOW() WHERE user_id = $1",
                row["user_id"], taken,
            )
        await conn.execute(
            "UPDATE support_refunds SET status = 'succeeded', updated_at = NOW(), error = NULL WHERE id = $1", refund_row_id,
        )
        price = money(json.loads(row["calc"])["price"] if isinstance(row["calc"], str) else row["calc"]["price"])
        retained = price - money(row["amount_rub"])
        details = {
            "refund_id": row["refund_id"], "yookassa_id": row["yookassa_id"], "amount_rub": str(row["amount_rub"]),
            "energy_taken": taken, "provider_status": provider_status, "payment_price": str(price),
            "npd": ("полный возврат — чек «Мой налог» аннулирует ежечасная синхронизация сама" if retained <= 0 else
                    f"частичный возврат: чек в «Мой налог» аннулируется и пробивается заново на {retained} ₽ "
                    "автоматически (npd_sync, в течение часа), руками ничего делать не нужно"),
        }
        await conn.fetchval(
            "INSERT INTO support_actions (ticket_id, admin_id, action, details) VALUES ($1, $2, 'refund', $3::jsonb) RETURNING id",
            row["ticket_id"], admin_id, json.dumps(details, ensure_ascii=False),
        )
    return {"already": False, "energy_taken": taken, "amount_rub": str(row["amount_rub"]), "npd": details["npd"],
            "refund_id": row["refund_id"], "provider_status": provider_status, "retained": str(retained)}


async def dismiss(pool: asyncpg.Pool, refund_row_id: int, admin_id: int) -> bool:
    updated = await pool.fetchval(
        "UPDATE support_refunds SET status = 'dismissed', admin_id = $2, updated_at = NOW() "
        "WHERE id = $1 AND status = 'proposed' RETURNING id", refund_row_id, admin_id,
    )
    return updated is not None
