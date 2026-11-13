"""T-Bank internet acquiring: signature, Init/GetState/Cancel, the notification webhook, the provider choice,
the shared settlement (same credit as YooKassa), the stale-pending worker and the inbox refund."""
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.routes import miniapp_pay, payments as payments_route
from app.core.config import settings
from app.db import users_repo, yookassa_repo
from app.services import payment_gateway, support_refund, tbank_acq
from app.workers import pending_payments
from tests.test_miniapp_pay import _seed_tiers, _tg_user  # noqa: F401

NOTIFY_URL = "/api/v1/payments/tbank/notify"
TERMINAL = "1234567890DEMO"
PASSWORD = "secret-pass"


@pytest.fixture(autouse=True)
def tbank_settings(monkeypatch):
    monkeypatch.setattr(settings, "TBANK_TERMINAL_KEY", TERMINAL)
    monkeypatch.setattr(settings, "TBANK_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "TBANK_API_BASE", "https://tbank.test/v2")
    monkeypatch.setattr(settings, "TBANK_NOTIFICATION_URL", "https://api.test/api/v1/payments/tbank/notify")
    monkeypatch.setattr(settings, "TBANK_SEND_RECEIPT", False)
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "yookassa")
    monkeypatch.setattr(settings, "TBANK_ROLLOUT_PERCENT", 0)
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", "")
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", 1)
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "yk-secret")
    monkeypatch.setattr(tbank_acq, "_RETRY_DELAYS", (0.0, 0.0))


# ── signature (examples of the docs: developer.tbank.ru/eacq/intro/developer/token and .../notification) ──

def test_token_of_the_docs_init_example():
    params = {
        "TerminalKey": "MerchantTerminalKey", "Amount": 19200, "OrderId": "00000",
        "Description": "Подарочная карта на 1000 рублей",
        "DATA": {"Phone": "+71234567890", "Email": "a@test.com"},
        "Receipt": {"Email": "a@test.ru", "Items": []},
    }
    assert tbank_acq.make_token(params, "11111111111111") == "72dd466f8ace0a37a1f740ce5fb78101712bc0665d91a8108c7c8a0ccd426db2"


def test_token_of_the_docs_notification_example():
    body = {
        "TerminalKey": "1234567890DEMO", "OrderId": "000000", "Success": True, "Status": "AUTHORIZED",
        "PaymentId": 0, "ErrorCode": "0", "Amount": 1111, "CardId": 0, "Pan": "200000******0000",
        "ExpDate": "1111", "RebillId": 0, "Data": {"x": "y"},
    }
    # the docs show the ids as zero-padded strings: "0000000", "000000"
    body.update(PaymentId="0000000", CardId="000000", RebillId="000000")
    token = "1c0964277d0213349243065a0d5b838b8e90d2d25f740d0f2767836e710e80c8"
    assert tbank_acq.make_token(body, "11111111111") == token
    assert tbank_acq.verify_notification({**body, "Token": token}, "11111111111")
    assert not tbank_acq.verify_notification({**body, "Amount": 1112, "Token": token}, "11111111111")
    assert not tbank_acq.verify_notification({**body, "Token": "0" * 64}, "11111111111")
    assert not tbank_acq.verify_notification(body, "11111111111")  # no token at all
    assert not tbank_acq.verify_notification({**body, "Token": token}, "")  # no password configured


def test_token_ignores_null_and_stringifies_booleans():
    assert tbank_acq.make_token({"A": None, "B": False, "C": 5}, "p") == tbank_acq.make_token({"B": "false", "C": "5"}, "p")


# ── client (HTTP mocked) ───────────────────────────────────────────

def bank(responses, calls):
    """A fake T-Bank: checks the Token of every request and answers by method name."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert tbank_acq.verify_notification(body, PASSWORD), "request is not signed"
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, body))
        answer = responses[method]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer)
    return httpx.MockTransport(handler)


INIT_OK = {"Success": True, "ErrorCode": "0", "TerminalKey": TERMINAL, "Status": "NEW", "PaymentId": "7700001",
           "OrderId": "x", "Amount": 29900, "PaymentURL": "https://securepay.tinkoff.ru/new/AbCd"}


@pytest.mark.asyncio
async def test_init_sends_kopecks_our_order_id_urls_and_no_receipt():
    calls = []
    result = await tbank_acq.create_payment(
        299, "YOUR_BRAND — пакет 10 энергии", 10, user_id=42, telegram_id=1001,
        return_url="https://t.me/bot", transport=bank({"Init": INIT_OK}, calls),
    )
    method, body = calls[0]
    assert method == "Init"
    assert body["TerminalKey"] == TERMINAL and body["Amount"] == 29900 and body["PayType"] == "O"
    assert body["OrderId"] == result["order_id"] and body["OrderId"].startswith("gl") and len(body["OrderId"]) <= 50
    assert body["NotificationURL"] == "https://api.test/api/v1/payments/tbank/notify"
    assert body["SuccessURL"] == body["FailURL"] == "https://t.me/bot"
    assert "Receipt" not in body
    assert body["DATA"] == {"user_id": "42", "generations": "10", "telegram_id": "1001"}
    assert PASSWORD not in json.dumps(body)
    assert (result["yookassa_id"], result["confirmation_url"], result["status"], result["provider"]) == (
        "7700001", "https://securepay.tinkoff.ru/new/AbCd?language=ru", "pending", "tbank")


@pytest.mark.asyncio
async def test_init_sends_a_receipt_only_when_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "TBANK_SEND_RECEIPT", True)
    calls = []
    await tbank_acq.create_payment(299, "Пакет", 10, user_id=1, transport=bank({"Init": INIT_OK}, calls))
    item = calls[0][1]["Receipt"]["Items"][0]
    assert (item["Price"], item["Amount"], item["Quantity"]) == (29900, 29900, 1)


@pytest.mark.asyncio
async def test_a_refused_init_is_an_error_with_the_bank_code():
    refusal = {"Success": False, "ErrorCode": "9999", "Message": "Неверные параметры", "Details": "Amount"}
    with pytest.raises(tbank_acq.TBankError) as caught:
        await tbank_acq.create_payment(299, "Пакет", 10, user_id=1, transport=bank({"Init": refusal}, []))
    assert caught.value.code == "9999" and not caught.value.unknown


@pytest.mark.asyncio
async def test_a_connect_failure_is_retried_but_a_lost_answer_is_unknown():
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=INIT_OK)

    result = await tbank_acq.create_payment(299, "Пакет", 10, user_id=1, transport=httpx.MockTransport(flaky))
    assert result["yookassa_id"] == "7700001" and len(attempts) == 2

    def lost(request):
        raise httpx.ReadTimeout("no answer")

    with pytest.raises(tbank_acq.TBankError) as caught:
        await tbank_acq.create_payment(299, "Пакет", 10, user_id=1, transport=httpx.MockTransport(lost))
    assert caught.value.unknown


@pytest.mark.asyncio
@pytest.mark.parametrize("bank_status,expected,paid", [
    ("CONFIRMED", "succeeded", True),
    ("AUTHORIZED", "pending", False), ("NEW", "pending", False), ("FORM_SHOWED", "pending", False),
    ("3DS_CHECKING", "pending", False), ("CONFIRMING", "pending", False),
    ("REJECTED", "canceled", False), ("CANCELED", "canceled", False), ("DEADLINE_EXPIRED", "canceled", False),
    ("AUTH_FAIL", "canceled", False),
])
async def test_get_state_maps_to_the_yookassa_shape(bank_status, expected, paid):
    state = {"Success": True, "ErrorCode": "0", "Status": bank_status, "PaymentId": "7700001", "OrderId": "ord1", "Amount": 29900}
    calls = []
    view = await tbank_acq.get_payment_status("7700001", transport=bank({"GetState": state}, calls))
    assert calls[0][1]["PaymentId"] == "7700001"
    assert (view["status"], view["paid"], view["amount_value"], view["currency"]) == (expected, paid, Decimal("299"), "RUB")


def test_matches_local_payment_needs_the_same_payment_order_amount_and_paid():
    local = {"yookassa_id": "7700001", "provider_order_id": "ord1", "amount_rubles": 299, "provider": "tbank"}
    good = {"yookassa_id": "7700001", "order_id": "ord1", "paid": True, "amount_value": Decimal("299")}
    assert tbank_acq.matches_local_payment(good, local)
    assert not tbank_acq.matches_local_payment({**good, "paid": False}, local)
    assert not tbank_acq.matches_local_payment({**good, "amount_value": Decimal("199")}, local)
    assert not tbank_acq.matches_local_payment({**good, "order_id": "other"}, local)
    assert not tbank_acq.matches_local_payment({**good, "yookassa_id": "1"}, local)
    assert not tbank_acq.matches_local_payment(good, {**local, "provider_order_id": None})


# ── which provider ─────────────────────────────────────────────────

def test_everybody_pays_through_yookassa_by_default():
    assert {payment_gateway.choose_provider(uid) for uid in range(1, 500)} == {"yookassa"}


def test_no_bank_keys_means_yookassa_whatever_the_flags_say(monkeypatch):
    monkeypatch.setattr(settings, "TBANK_PASSWORD", "")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "tbank")
    monkeypatch.setattr(settings, "TBANK_ROLLOUT_PERCENT", 100)
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", "7001")
    assert payment_gateway.choose_provider(7001) == "yookassa"


def test_the_force_list_is_always_tbank_the_rest_follow_the_flag(monkeypatch):
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", "7001, 7003 ,junk")
    assert payment_gateway.choose_provider(7001) == payment_gateway.choose_provider(7003) == "tbank"
    assert payment_gateway.choose_provider(8956) == "yookassa"
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "TBank")
    assert payment_gateway.choose_provider(8956) == "tbank"


def test_the_percentage_rollout_is_stable_monotonic_and_roughly_even(monkeypatch):
    ids = range(1, 2001)
    chosen = {}
    for percent in (0, 10, 50, 100):
        monkeypatch.setattr(settings, "TBANK_ROLLOUT_PERCENT", percent)
        chosen[percent] = {uid for uid in ids if payment_gateway.choose_provider(uid) == "tbank"}
        assert chosen[percent] == {uid for uid in ids if payment_gateway.choose_provider(uid) == "tbank"}  # stable
    assert chosen[0] == set() and len(chosen[100]) == 2000
    assert chosen[10] <= chosen[50] <= chosen[100]  # raising the percent only adds users
    assert 140 <= len(chosen[10]) <= 260 and 900 <= len(chosen[50]) <= 1100
    monkeypatch.setattr(settings, "TBANK_ROLLOUT_PERCENT", 500)  # nonsense is clamped
    assert len({uid for uid in ids if payment_gateway.choose_provider(uid) == "tbank"}) == 2000


@pytest.mark.asyncio
async def test_a_failing_tbank_checkout_raises_and_never_touches_yookassa(monkeypatch):
    """No automatic fallback: the buyer sees an error (the caller answers 5xx), YooKassa is not asked."""
    async def down(*args, **kwargs):
        raise tbank_acq.TBankError("down")

    async def yookassa(**kwargs):
        raise AssertionError("YooKassa must not be called when T-Bank fails")

    monkeypatch.setattr(tbank_acq, "create_payment", down)
    monkeypatch.setattr(payment_gateway.yookassa_service, "create_sbp_payment", yookassa)
    with pytest.raises(tbank_acq.TBankError):
        await payment_gateway.create_payment("tbank", amount_rubles=299, description="d", generations=10, user_id=1)


# ── data helpers ───────────────────────────────────────────────────

async def seed_user(pool, telegram_id, balance=3):
    user_id = await pool.fetchval("INSERT INTO users (telegram_id) VALUES ($1) RETURNING id", telegram_id)
    await pool.execute("INSERT INTO balances (user_id, generations_remaining) VALUES ($1, $2)", user_id, balance)
    return user_id


async def seed_payment(pool, user_id, telegram_id, *, provider="tbank", pid="7700001", order="ord1", amount=299,
                       generations=10, platform="telegram", bonus=0, offer=None, status="pending"):
    await pool.execute(
        """
        INSERT INTO yookassa_payments (user_id, telegram_id, yookassa_id, amount_rubles, generations, platform,
                                       bonus_generations, offer_slug, provider, provider_order_id, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        user_id, telegram_id, pid, amount, generations, platform, bonus, offer, provider,
        order if provider == "tbank" else None, status,
    )


def notification(status="CONFIRMED", *, pid="7700001", order="ord1", amount=29900, **over):
    body = {"TerminalKey": TERMINAL, "OrderId": order, "Success": True, "Status": status, "PaymentId": int(pid),
            "ErrorCode": "0", "Amount": amount, "CardId": 4001, "Pan": "430000******0777", "ExpDate": "1230"}
    body.update(over)
    body["Token"] = tbank_acq.make_token(body, PASSWORD)
    return body


async def balance_of(pool, user_id):
    return await pool.fetchval("SELECT generations_remaining FROM balances WHERE user_id = $1", user_id)


async def row_of(pool, pid="7700001"):
    return await pool.fetchrow("SELECT * FROM yookassa_payments WHERE yookassa_id = $1", pid)


def state_with_source(source, *, pid="7700001", order="ord1"):
    """A fake ``tbank_acq.get_payment_status`` that answers like GetState of a paid payment with ``Params[Source]``."""
    async def state(payment_id, **kwargs):
        params = [{"Key": "Route", "Value": "ACQ"}] + ([{"Key": "Source", "Value": source}] if source else [])
        return tbank_acq.remote_view({"Success": True, "ErrorCode": "0", "Status": "CONFIRMED", "PaymentId": pid,
                                      "OrderId": order, "Amount": 29900, "Params": params})
    return state


# ── webhook ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_confirmed_notification_credits_once_and_answers_ok(client, clean_db, monkeypatch):
    user = await seed_user(clean_db, 90001)
    await seed_payment(clean_db, user, 90001)
    monkeypatch.setattr(tbank_acq, "get_payment_status", state_with_source("cards"))

    answer = await client.post(NOTIFY_URL, json=notification())
    assert (answer.status_code, answer.text) == (200, "OK")
    assert await balance_of(clean_db, user) == 13
    row = await row_of(clean_db)
    assert row["status"] == "succeeded" and row["completed_at"] is not None and row["provider"] == "tbank"
    assert (row["card_first6"], row["card_last4"], row["payment_method_type"]) == ("430000", "0777", "bank_card")
    assert await clean_db.fetchval("SELECT comment FROM payments WHERE user_id = $1", user) == "T-Bank: 299₽"
    assert await clean_db.fetchval("SELECT has_purchased FROM users WHERE id = $1", user) is True

    # the same notification again, and AUTHORIZED that T-Bank sends next to CONFIRMED: nothing changes
    for repeat in (notification(), notification("AUTHORIZED"), notification()):
        again = await client.post(NOTIFY_URL, json=repeat)
        assert (again.status_code, again.text) == (200, "OK")
    assert await balance_of(clean_db, user) == 13
    assert await clean_db.fetchval("SELECT COUNT(*) FROM payments WHERE user_id = $1", user) == 1


@pytest.mark.asyncio
async def test_a_forged_or_tampered_notification_changes_nothing(client, clean_db):
    user = await seed_user(clean_db, 90002)
    await seed_payment(clean_db, user, 90002)

    forged = notification()
    forged["Token"] = "0" * 64
    tampered = notification()
    tampered["Amount"] = 1  # signed for 29900
    unsigned = {k: v for k, v in notification().items() if k != "Token"}
    for body in (forged, tampered, unsigned):
        assert (await client.post(NOTIFY_URL, json=body)).status_code == 403
    assert (await client.post(NOTIFY_URL, content=b"not json", headers={"Content-Type": "application/json"})).status_code == 400
    assert await balance_of(clean_db, user) == 3
    assert (await row_of(clean_db))["status"] == "pending"


@pytest.mark.asyncio
async def test_a_signed_notification_for_another_amount_or_order_credits_nothing(client, clean_db):
    user = await seed_user(clean_db, 90003)
    await seed_payment(clean_db, user, 90003)

    assert (await client.post(NOTIFY_URL, json=notification(amount=100))).text == "OK"  # signed, but not our amount
    assert (await client.post(NOTIFY_URL, json=notification(order="someone-else"))).text == "OK"
    assert (await client.post(NOTIFY_URL, json=notification(pid="999"))).text == "OK"  # unknown payment
    assert (await client.post(NOTIFY_URL, json=notification(TerminalKey="OTHER"))).text == "OK"
    assert await balance_of(clean_db, user) == 3
    assert (await row_of(clean_db))["status"] == "pending"


@pytest.mark.asyncio
async def test_failed_statuses_close_the_payment_and_a_later_confirmation_reopens_it(client, clean_db):
    user = await seed_user(clean_db, 90004)
    await seed_payment(clean_db, user, 90004)

    assert (await client.post(NOTIFY_URL, json=notification("REJECTED", Success=False, ErrorCode="1051"))).text == "OK"
    row = await row_of(clean_db)
    assert row["status"] == "canceled" and await balance_of(clean_db, user) == 3

    # the buyer retried on the same form with another card
    assert (await client.post(NOTIFY_URL, json=notification("CONFIRMED"))).text == "OK"
    assert (await row_of(clean_db))["status"] == "succeeded" and await balance_of(clean_db, user) == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("bank_status", ["CANCELED", "DEADLINE_EXPIRED"])
async def test_canceled_and_expired_notifications_close_the_payment(client, clean_db, bank_status):
    user = await seed_user(clean_db, 90005)
    await seed_payment(clean_db, user, 90005)
    await client.post(NOTIFY_URL, json=notification(bank_status))
    assert (await row_of(clean_db))["status"] == "canceled"
    # a canceled payment never turns into a credit by itself, and a success notification of a settled one stays settled
    assert await balance_of(clean_db, user) == 3


@pytest.mark.asyncio
async def test_refund_notifications_are_recorded_without_touching_the_status(client, clean_db):
    user = await seed_user(clean_db, 90006)
    await seed_payment(clean_db, user, 90006)
    await client.post(NOTIFY_URL, json=notification("CONFIRMED"))

    await client.post(NOTIFY_URL, json=notification("PARTIAL_REFUNDED", amount=10000))  # amount of a partial refund is unknown
    row = await row_of(clean_db)
    assert (row["status"], row["refunded_rubles"]) == ("succeeded", Decimal("0"))

    await client.post(NOTIFY_URL, json=notification("REFUNDED"))
    row = await row_of(clean_db)
    assert (row["status"], row["refunded_rubles"]) == ("succeeded", Decimal("299"))


@pytest.mark.asyncio
async def test_notify_is_503_without_keys_and_the_yookassa_webhook_ignores_tbank_payments(client, clean_db, monkeypatch):
    user = await seed_user(clean_db, 90007)
    await seed_payment(clean_db, user, 90007)

    class FakeRequest:
        async def json(self):
            return {"event": "payment.succeeded", "object": {"id": "7700001"}}

    provider = AsyncMock()
    monkeypatch.setattr(payments_route.yookassa_service, "get_payment_status", provider)
    assert await payments_route.yookassa_webhook(FakeRequest(), clean_db) == {"ok": True}
    provider.assert_not_called()

    monkeypatch.setattr(settings, "TBANK_PASSWORD", "")
    assert (await client.post(NOTIFY_URL, json=notification())).status_code == 503


# ── the credit is the YooKassa credit ──────────────────────────────

@pytest.mark.asyncio
async def test_a_tbank_payment_credits_exactly_what_the_same_yookassa_package_does(client, clean_db):
    """Menu bonus + standing offer + first-purchase bonus on a *_bonus platform, same package, both providers."""
    yk_user, tb_user = await seed_user(clean_db, 90010, 3), await seed_user(clean_db, 90011, 3)
    for platform, bonus, offer in (("telegram_bonus", 0, None), ("telegram", 5, "ladder_199")):
        await clean_db.execute("UPDATE balances SET generations_remaining = 3")
        await clean_db.execute("UPDATE users SET has_purchased = FALSE")
        await clean_db.execute("DELETE FROM yookassa_payments")
        await clean_db.execute("DELETE FROM payments")
        await seed_payment(clean_db, yk_user, 90010, provider="yookassa", pid="yk-uuid-1", platform=platform,
                           bonus=bonus, offer=offer)
        await seed_payment(clean_db, tb_user, 90011, provider="tbank", pid="7700002", order="ord2", platform=platform,
                           bonus=bonus, offer=offer)

        yk = await yookassa_repo.settle_succeeded_payment(clean_db, "yk-uuid-1")
        assert (await client.post(NOTIFY_URL, json=notification(pid="7700002", order="ord2"))).text == "OK"

        assert await balance_of(clean_db, tb_user) == await balance_of(clean_db, yk_user) == yk["new_balance"]
        comments = {r["user_id"]: r["comment"] for r in await clean_db.fetch("SELECT user_id, comment FROM payments")}
        assert comments[tb_user].replace("T-Bank", "YooKassa SBP") == comments[yk_user]
        assert yk["credited_generations"] == 10 + (5 if platform == "telegram_bonus" else bonus)


# ── checkout: bots and Mini App create the payment at the chosen provider ──

def fake_tbank_create(created):
    async def create(amount_rubles, description, generations, **kwargs):
        created.append({"amount_rubles": amount_rubles, "generations": generations, **kwargs})
        n = len(created)
        return {"yookassa_id": f"77100{n}", "confirmation_url": f"https://securepay.test/pay/{n}",
                "status": "pending", "order_id": f"glorder{n}", "provider": "tbank"}
    return create


@pytest.mark.asyncio
async def test_the_tg_bot_create_endpoint_pays_through_the_flagged_provider(client, clean_db, monkeypatch):
    await _seed_tiers(clean_db)
    user = await _tg_user(clean_db, 1001)
    user_id = user["user"]["id"]
    created, yk_calls = [], []
    monkeypatch.setattr(tbank_acq, "create_payment", fake_tbank_create(created))
    monkeypatch.setattr(payments_route.analytics, "track", AsyncMock())

    async def yk_create(**kwargs):
        yk_calls.append(kwargs)
        return {"yookassa_id": "yk-new", "confirmation_url": "https://yk/pay", "status": "pending"}

    monkeypatch.setattr(payments_route.yookassa_service, "create_sbp_payment", yk_create)
    headers = {"Authorization": "Bearer test-secret-token"}
    body = {"telegram_id": 1001, "generations": 10, "amount_rubles": 299}

    plain = (await client.post("/api/v1/payments/yookassa/create", json=body, headers=headers)).json()
    assert plain["yookassa_id"] == "yk-new" and not created
    assert (await row_of(clean_db, "yk-new"))["provider"] == "yookassa"

    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", str(user_id))
    answer = (await client.post("/api/v1/payments/yookassa/create", json=body, headers=headers)).json()
    assert answer["yookassa_id"] == "771001" and answer["confirmation_url"] == "https://securepay.test/pay/1"
    assert len(yk_calls) == 1
    row = await row_of(clean_db, "771001")
    assert (row["provider"], row["provider_order_id"], row["status"], row["amount_rubles"]) == ("tbank", "glorder1", "pending", 299)
    assert created[0]["user_id"] == user_id and created[0]["telegram_id"] == 1001

    # the bot's own "check payment" call settles a T-Bank payment through GetState
    async def paid_state(payment_id, **kwargs):
        return {"yookassa_id": payment_id, "order_id": "glorder1", "status": "succeeded", "paid": True,
                "amount_value": Decimal("299"), "currency": "RUB", "metadata": {}, "payment_method": None}

    monkeypatch.setattr(tbank_acq, "get_payment_status", paid_state)
    status = (await client.get("/api/v1/payments/yookassa/status", params={"telegram_id": 1001, "yookassa_id": "771001"}, headers=headers)).json()
    assert (status["found"], status["status"]) == (True, "succeeded")
    assert (await row_of(clean_db, "771001"))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_the_mini_app_gets_the_bank_page_for_every_button_and_polls_it(clean_db, monkeypatch):
    monkeypatch.setattr(settings, "TBANK_BUTTONS", False)  # the single-button sheet (the buttons: tests/test_tbank_buttons.py)
    await _seed_tiers(clean_db)
    user = await _tg_user(clean_db, 1002)
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", str(user["user"]["id"]))
    created = []
    monkeypatch.setattr(tbank_acq, "create_payment", fake_tbank_create(created))
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())

    def create(**body):
        request = miniapp_pay.MiniAppYookassaCreateRequest(**{"generations": 10, "amount_rubles": 299, **body})
        return miniapp_pay.create_embedded_payment(request, miniapp_user=user, is_preview=False, pool=clean_db)

    for method in ("widget", "sbp", "sberbank", "tinkoff_bank", "bank_card"):
        result = await create(method=method)
        assert result["confirmation_type"] == "redirect" and result["confirmation_token"] is None
        assert result["confirmation_url"].startswith("https://securepay.test/pay/")
    # the form ends on our page that tells the Mini App (SuccessURL / FailURL), not on the bot link
    assert created[0]["return_url"].endswith("/pay-return?p=tg&ok=1") and created[0]["fail_url"].endswith("/pay-return?p=tg&ok=0")
    assert created[0]["extra_metadata"] == {"miniapp_method": "widget"}

    paid = AsyncMock(return_value={"yookassa_id": "771001", "order_id": "glorder1", "status": "succeeded", "paid": True,
                                   "amount_value": Decimal("299"), "currency": "RUB", "metadata": {}, "payment_method": None})
    monkeypatch.setattr(tbank_acq, "get_payment_status", paid)

    class FakeRequest:
        class app:
            class state:
                notifications_client = None

    answer = await miniapp_pay.get_payment_status("771001", FakeRequest(), miniapp_user=user, pool=clean_db)
    assert answer["status"] == "succeeded" and answer["credited_generations"] == 10


@pytest.mark.asyncio
async def test_the_mini_app_answers_502_when_tbank_is_down_and_stores_nothing(clean_db, monkeypatch):
    await _seed_tiers(clean_db)
    user = await _tg_user(clean_db, 1003)
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "tbank")

    async def down(*args, **kwargs):
        raise tbank_acq.TBankError("down", unknown=True)

    async def yookassa(**kwargs):
        raise AssertionError("YooKassa must not be called when T-Bank fails")

    monkeypatch.setattr(tbank_acq, "create_payment", down)
    monkeypatch.setattr(payment_gateway.yookassa_service, "create_sbp_payment", yookassa)
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())
    request = miniapp_pay.MiniAppYookassaCreateRequest(generations=10, amount_rubles=299)
    with pytest.raises(HTTPException) as failed:
        await miniapp_pay.create_embedded_payment(request, miniapp_user=user, is_preview=False, pool=clean_db)
    assert failed.value.status_code == 502
    assert await clean_db.fetchval("SELECT count(*) FROM yookassa_payments") == 0


# ── how the payment was paid (payment_method_type) ─────────────────

@pytest.mark.parametrize("source,expected", [
    ("SberPay", "sberbank"), ("qrsbp", "sbp"), ("TinkoffPay", "tinkoff_bank"), ("cards", "bank_card"),
    ("MirPay", "mir_pay"), ("sberpay", "sberbank"), (" QRSBP ", "sbp"),
    ("AlfaPay", "tbank_alfapay"), ("BNPL", "tbank_bnpl"), ("Installment", "tbank_installment"),
    ("qrdigitalruble", "tbank_qrdigitalruble"), ("Something New", "tbank_something new"),
    (None, None), ("", None),
])
def test_the_source_of_a_payment_maps_to_the_yookassa_type(source, expected):
    assert tbank_acq.method_type_of_source(source) == expected


def test_get_state_params_give_the_type_and_no_card():
    paid = {"Status": "CONFIRMED", "PaymentId": "1", "OrderId": "o", "Amount": 100,
            "Params": [{"Key": "Route", "Value": "SBER"}, {"Key": "Source", "Value": "SberPay"}]}
    assert tbank_acq.remote_view(paid)["payment_method"] == {
        "type": "sberbank", "card_first6": None, "card_last4": None, "card_type": None, "card_country": None}
    # an unpaid payment has only the route, an old answer or a notification has no Params at all
    assert tbank_acq.remote_view({**paid, "Params": [{"Key": "Route", "Value": "ACQ"}]})["payment_method"] is None
    assert tbank_acq.remote_view({k: v for k, v in paid.items() if k != "Params"})["payment_method"] is None
    assert tbank_acq.remote_view({**paid, "Params": {"Source": "SberPay"}})["payment_method"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source,expected", [("SberPay", "sberbank"), ("qrsbp", "sbp"), ("TinkoffPay", "tinkoff_bank")])
async def test_a_wallet_payment_gets_its_type_from_get_state_and_keeps_the_card_mask(client, clean_db, monkeypatch, source, expected):
    user = await seed_user(clean_db, 90040)
    await seed_payment(clean_db, user, 90040)
    monkeypatch.setattr(tbank_acq, "get_payment_status", state_with_source(source))

    assert (await client.post(NOTIFY_URL, json=notification())).text == "OK"
    row = await row_of(clean_db)
    assert (row["status"], row["payment_method_type"]) == ("succeeded", expected)
    assert (row["card_first6"], row["card_last4"]) == ("430000", "0777")  # the antifraud card is not lost


@pytest.mark.asyncio
async def test_a_failing_get_state_neither_holds_back_the_credit_nor_the_answer_and_a_repeat_fills_the_type(client, clean_db, monkeypatch):
    user = await seed_user(clean_db, 90041)
    await seed_payment(clean_db, user, 90041)
    calls = []

    async def down(payment_id, **kwargs):
        calls.append(payment_id)
        raise tbank_acq.TBankError("down", unknown=True)

    monkeypatch.setattr(tbank_acq, "get_payment_status", down)
    answer = await client.post(NOTIFY_URL, json=notification())
    assert (answer.status_code, answer.text) == (200, "OK")
    row = await row_of(clean_db)
    assert (row["status"], row["payment_method_type"], row["card_last4"]) == ("succeeded", None, "0777")
    assert await balance_of(clean_db, user) == 13

    # T-Bank repeats the notification: the type is asked again, the credit stays single
    monkeypatch.setattr(tbank_acq, "get_payment_status", state_with_source("SberPay"))
    assert (await client.post(NOTIFY_URL, json=notification())).text == "OK"
    assert (await row_of(clean_db))["payment_method_type"] == "sberbank"
    assert await balance_of(clean_db, user) == 13

    # once the type is there the bank is not asked any more
    monkeypatch.setattr(tbank_acq, "get_payment_status", down)
    calls.clear()
    assert (await client.post(NOTIFY_URL, json=notification())).text == "OK"
    assert calls == []


@pytest.mark.asyncio
async def test_a_slow_get_state_is_cut_off(client, clean_db, monkeypatch):
    import asyncio
    user = await seed_user(clean_db, 90042)
    await seed_payment(clean_db, user, 90042)

    async def stuck(payment_id, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(tbank_acq, "get_payment_status", stuck)
    monkeypatch.setattr(payments_route, "TBANK_METHOD_TIMEOUT", 0.05)
    assert (await client.post(NOTIFY_URL, json=notification())).text == "OK"
    assert (await row_of(clean_db))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_the_card_and_the_type_complete_each_other_in_any_order(clean_db):
    user = await seed_user(clean_db, 90043)
    card = tbank_acq.notification_card(notification())
    assert card["type"] is None and card["card_last4"] == "0777"
    kind = tbank_acq.remote_view({"Status": "CONFIRMED", "PaymentId": "1", "OrderId": "o", "Amount": 1,
                                  "Params": [{"Key": "Source", "Value": "TinkoffPay"}]})["payment_method"]

    await seed_payment(clean_db, user, 90043, pid="7700051", order="o51")
    await yookassa_repo.save_payment_method(clean_db, "7700051", card)
    await yookassa_repo.save_payment_method(clean_db, "7700051", kind)
    await seed_payment(clean_db, user, 90043, pid="7700052", order="o52")
    await yookassa_repo.save_payment_method(clean_db, "7700052", kind)
    await yookassa_repo.save_payment_method(clean_db, "7700052", card)
    for pid in ("7700051", "7700052"):
        row = await row_of(clean_db, pid)
        assert (row["payment_method_type"], row["card_first6"], row["card_last4"]) == ("tinkoff_bank", "430000", "0777")

    # a later answer never rewrites what is there
    await yookassa_repo.save_payment_method(clean_db, "7700052", {"type": "sbp", "card_first6": "999999", "card_last4": "1111"})
    row = await row_of(clean_db, "7700052")
    assert (row["payment_method_type"], row["card_last4"]) == ("tinkoff_bank", "0777")


@pytest.mark.asyncio
async def test_the_status_poll_worker_writes_the_type_of_a_tbank_payment(clean_db, monkeypatch):
    user = await seed_user(clean_db, 90044)
    await seed_payment(clean_db, user, 90044, pid="7700061", order="o61")
    await clean_db.execute("UPDATE yookassa_payments SET created_at = NOW() - INTERVAL '10 minutes'")
    monkeypatch.setattr(tbank_acq, "get_payment_status", state_with_source("qrsbp", pid="7700061", order="o61"))
    monkeypatch.setattr(pending_payments, "get_pool", lambda: clean_db)
    monkeypatch.setattr(pending_payments.analytics, "track", AsyncMock())
    await pending_payments._sweep(None, None)
    row = await row_of(clean_db, "7700061")
    assert (row["status"], row["payment_method_type"]) == ("succeeded", "sbp")


def load_backfill():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_tbank_method.py"
    spec = importlib.util.spec_from_file_location("backfill_tbank_method", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_backfill_is_a_dry_run_until_apply_and_only_touches_empty_tbank_types(clean_db, monkeypatch, capsys):
    from tests.conftest import TEST_DB_URL
    backfill = load_backfill()
    monkeypatch.setattr(backfill, "PAUSE_SECONDS", 0)
    user = await seed_user(clean_db, 90045)
    sources = {"8100001": "SberPay", "8100002": "qrsbp", "8100003": None, "8100004": "cards", "8100005": "TinkoffPay"}
    for n, pid in enumerate(sources, 1):
        await seed_payment(clean_db, user, 90045, pid=pid, order=f"b{n}", status="succeeded")
    await seed_payment(clean_db, user, 90045, pid="8100006", order="b6", status="refunded")
    await seed_payment(clean_db, user, 90045, pid="8100007", order="b7", status="pending")         # not paid: skipped
    await seed_payment(clean_db, user, 90045, pid="8100008", order="b8", status="succeeded")       # already typed
    await seed_payment(clean_db, user, 90045, pid="ykp-1", provider="yookassa", status="succeeded")  # not T-Bank
    await seed_payment(clean_db, user, 90045, pid="8100009", order="b9", status="succeeded")       # a card-like Pan wallet
    await clean_db.execute("UPDATE yookassa_payments SET payment_method_type = 'sbp' WHERE yookassa_id = '8100008'")
    await clean_db.execute("UPDATE yookassa_payments SET payment_method_type = 'bank_card', card_last4 = '0777' "
                           "WHERE yookassa_id = '8100009'")
    sources.update({"8100006": "SberPay", "8100008": "SberPay", "8100009": "TinkoffPay"})
    asked = []

    async def state(payment_id, **kwargs):
        asked.append(payment_id)
        order = await clean_db.fetchval("SELECT provider_order_id FROM yookassa_payments WHERE yookassa_id = $1", payment_id)
        return await state_with_source(sources[payment_id], pid=payment_id, order=order)(payment_id)

    monkeypatch.setattr(tbank_acq, "get_payment_status", state)

    async def types():
        return {r["yookassa_id"]: r["payment_method_type"] for r in await clean_db.fetch(
            "SELECT yookassa_id, payment_method_type FROM yookassa_payments ORDER BY yookassa_id")}

    before = await types()
    assert await backfill.run(TEST_DB_URL, apply=False, recheck_cards=False, limit=None) == 0
    assert await types() == before
    assert sorted(asked) == ["8100001", "8100002", "8100003", "8100004", "8100005", "8100006"]

    assert await backfill.run(TEST_DB_URL, apply=True, recheck_cards=False, limit=None) == 0
    after = await types()
    assert after["8100001"] == "sberbank" and after["8100002"] == "sbp" and after["8100004"] == "bank_card"
    assert after["8100005"] == "tinkoff_bank" and after["8100006"] == "sberbank"
    assert after["8100003"] is None                      # no Source: left empty
    assert after["8100007"] is None and after["ykp-1"] is None  # pending and YooKassa rows are not looked at
    assert after["8100008"] == "sbp" and after["8100009"] == "bank_card"  # never overwritten without --recheck-cards

    asked.clear()
    assert await backfill.run(TEST_DB_URL, apply=True, recheck_cards=True, limit=None) == 0
    assert (await types())["8100009"] == "tinkoff_bank"  # the card mask was a wallet
    assert (await row_of(clean_db, "8100009"))["card_last4"] == "0777"
    assert "8100008" not in asked and "8100001" not in asked


# ── the cover for a lost notification ──────────────────────────────

@pytest.mark.asyncio
async def test_the_stale_pending_worker_settles_and_closes_tbank_payments_by_get_state(clean_db, monkeypatch):
    paid_user, failed_user = await seed_user(clean_db, 90020), await seed_user(clean_db, 90021)
    await seed_payment(clean_db, paid_user, 90020, pid="7700011", order="o11")
    await seed_payment(clean_db, failed_user, 90021, pid="7700012", order="o12")
    await clean_db.execute("UPDATE yookassa_payments SET created_at = NOW() - INTERVAL '10 minutes'")

    async def state(payment_id, **kwargs):
        paid = payment_id == "7700011"
        return {"yookassa_id": payment_id, "order_id": "o11" if paid else "o12", "paid": paid, "currency": "RUB",
                "status": "succeeded" if paid else "canceled", "amount_value": Decimal("299"), "metadata": {}, "payment_method": None}

    monkeypatch.setattr(tbank_acq, "get_payment_status", state)
    monkeypatch.setattr(pending_payments, "get_pool", lambda: clean_db)
    monkeypatch.setattr(pending_payments.analytics, "track", AsyncMock())
    await pending_payments._sweep(None, None)

    assert (await row_of(clean_db, "7700011"))["status"] == "succeeded" and await balance_of(clean_db, paid_user) == 13
    assert (await row_of(clean_db, "7700012"))["status"] == "canceled" and await balance_of(clean_db, failed_user) == 3


# ── refunds ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_returns_part_of_a_payment_and_records_it(clean_db):
    user = await seed_user(clean_db, 90030)
    await seed_payment(clean_db, user, 90030, status="succeeded")
    calls = []
    answer = {"Success": True, "ErrorCode": "0", "Status": "PARTIAL_REFUNDED", "PaymentId": "7700001",
              "OriginalAmount": 29900, "NewAmount": 17940}
    payment = dict(await row_of(clean_db))

    result = await support_refund.tbank_cancel(
        clean_db, payment=payment, amount=Decimal("119.60"), retry_after_unknown=False,
        transport=bank({"Cancel": answer}, calls),
    )
    method, body = calls[0]
    assert method == "Cancel" and body["PaymentId"] == "7700001" and body["Amount"] == 11960
    assert result["id"] == "tbank:7700001"
    row = await row_of(clean_db)
    assert (row["status"], row["refunded_rubles"]) == ("succeeded", Decimal("119.60"))


@pytest.mark.asyncio
async def test_a_retry_after_a_lost_cancel_answer_does_not_refund_twice(clean_db):
    user = await seed_user(clean_db, 90031)
    await seed_payment(clean_db, user, 90031, status="succeeded")
    calls = []
    state = {"Success": True, "ErrorCode": "0", "Status": "PARTIAL_REFUNDED", "PaymentId": "7700001", "OrderId": "ord1", "Amount": 29900}
    result = await support_refund.tbank_cancel(
        clean_db, payment=dict(await row_of(clean_db)), amount=Decimal("119.60"), retry_after_unknown=True,
        transport=bank({"GetState": state, "Cancel": AssertionError("must not call Cancel again")}, calls),
    )
    assert [c[0] for c in calls] == ["GetState"] and result["already"] is True
    assert (await row_of(clean_db))["refunded_rubles"] == Decimal("119.60")

    # still confirmed at the bank: the first Cancel never arrived, so it is sent now
    other = await seed_user(clean_db, 90032)
    await seed_payment(clean_db, other, 90032, pid="7700002", order="ord2", status="succeeded")
    calls.clear()
    state["Status"] = "CONFIRMED"
    cancel = {"Success": True, "ErrorCode": "0", "Status": "PARTIAL_REFUNDED", "PaymentId": "7700002", "OriginalAmount": 29900, "NewAmount": 17940}
    await support_refund.tbank_cancel(
        clean_db, payment=dict(await row_of(clean_db, "7700002")), amount=Decimal("119.60"), retry_after_unknown=True,
        transport=bank({"GetState": state, "Cancel": cancel}, calls),
    )
    assert [c[0] for c in calls] == ["GetState", "Cancel"]


@pytest.mark.asyncio
async def test_the_refund_is_refused_by_the_bank_without_changing_the_row(clean_db):
    user = await seed_user(clean_db, 90033)
    await seed_payment(clean_db, user, 90033, status="succeeded")
    refusal = {"Success": False, "ErrorCode": "331", "Message": "Неверный статус платежа"}
    with pytest.raises(support_refund.RefundError) as caught:
        await support_refund.tbank_cancel(
            clean_db, payment=dict(await row_of(clean_db)), amount=Decimal("100"), retry_after_unknown=False,
            transport=bank({"Cancel": refusal}, []),
        )
    assert not caught.value.unknown
    assert (await row_of(clean_db))["refunded_rubles"] == 0


@pytest.mark.usefixtures("inbox_schema")
@pytest.mark.asyncio
async def test_the_inbox_button_refunds_a_tbank_payment_through_cancel(clean_db, monkeypatch):
    from tests.test_support_autoreply import customer_says_direct
    from tests.test_support_inbox import ADMIN_ID, make_user

    pool = clean_db
    await pool.execute("TRUNCATE support_inbox_state, support_actions, support_drafts, support_messages, support_tickets RESTART IDENTITY CASCADE")
    user = await make_user(pool, balance=4)
    payment = await pool.fetchrow(
        """
        INSERT INTO yookassa_payments (user_id, yookassa_id, amount_rubles, generations, status, provider, provider_order_id,
                                       created_at, completed_at)
        VALUES ($1, '7700077', 299, 10, 'succeeded', 'tbank', 'ordR', NOW() - INTERVAL '1 hour', NOW() - INTERVAL '1 hour')
        RETURNING id, yookassa_id
        """,
        user,
    )
    await customer_says_direct(pool, "верните деньги")
    ticket_id = await pool.fetchval("SELECT id FROM support_tickets ORDER BY id DESC LIMIT 1")
    row = await pool.fetchrow(
        """
        INSERT INTO support_refunds (ticket_id, user_id, payment_id, yookassa_id, amount_rub, energy, calc, idempotence_key)
        VALUES ($1, $2, $3, $4, 119.60, 4, $5::jsonb, 'idem-1') RETURNING *
        """,
        ticket_id, user, payment["id"], payment["yookassa_id"], json.dumps({"price": "299.00", "package": 10, "unused": 4}),
    )
    calls = []
    cancel = {"Success": True, "ErrorCode": "0", "Status": "PARTIAL_REFUNDED", "PaymentId": "7700077",
              "OriginalAmount": 29900, "NewAmount": 17940}
    no_yookassa = AsyncMock(side_effect=AssertionError("YooKassa must not be called for a T-Bank payment"))
    monkeypatch.setattr(support_refund, "fetch_payment", no_yookassa)
    monkeypatch.setattr(support_refund, "create_refund", no_yookassa)

    result = await support_refund.execute_refund(
        pool, refund_row_id=row["id"], admin_id=ADMIN_ID, expected_amount="119.60", transport=bank({"Cancel": cancel}, calls),
    )
    assert calls[0][1]["Amount"] == 11960 and calls[0][1]["PaymentId"] == "7700077"
    assert result["energy_taken"] == 4 and result["amount_rub"] == "119.60"
    saved = await pool.fetchrow("SELECT status, refund_id FROM support_refunds WHERE id = $1", row["id"])
    assert (saved["status"], saved["refund_id"]) == ("succeeded", "tbank:7700077")
    assert await pool.fetchval("SELECT refunded_rubles FROM yookassa_payments WHERE id = $1", payment["id"]) == Decimal("119.60")
    assert await pool.fetchval("SELECT generations_remaining FROM balances WHERE user_id = $1", user) == 0
    with pytest.raises(support_refund.RefundError, match="уже выполнен"):
        await support_refund.execute_refund(pool, refund_row_id=row["id"], admin_id=ADMIN_ID, expected_amount="119.60",
                                            transport=bank({"Cancel": cancel}, calls))
    assert len(calls) == 1


def test_payment_form_link_is_always_russian():
    # Форма Т-Банка берёт язык телефона; на английском телефоне без language=ru она английская.
    assert tbank_acq._with_russian_form("https://pay.tbank-online.com/AbCd") == "https://pay.tbank-online.com/AbCd?language=ru"
    assert tbank_acq._with_russian_form("https://pay.tbank-online.com/AbCd?x=1&language=en") == "https://pay.tbank-online.com/AbCd?x=1&language=ru"
