"""T-Bank on the preview: test terminal (nothing counts as money), only the owner's accounts, the form inside the app."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.routes import miniapp_pay, payments as payments_route
from app.core.config import settings
from app.db import yookassa_repo
from app.services import payment_gateway, tbank_acq
from fastapi import HTTPException
from tests.test_miniapp_pay import _seed_tiers, _tg_user  # noqa: F401
from tests.test_tbank_acquiring import (  # noqa: F401
    NOTIFY_URL, balance_of, notification, row_of, seed_payment, seed_user, tbank_settings,
)

TEST_ORDER = "gltest" + "ab" * 13


@pytest.fixture
def test_mode(monkeypatch):
    monkeypatch.setattr(settings, "TBANK_TEST_MODE", True)
    monkeypatch.setattr(settings, "TBANK_EMBED", True)
    monkeypatch.setattr(settings, "TBANK_RETURN_URL", "https://app.test/res-api/v1/miniapp/pay-return")
    monkeypatch.setattr(settings, "MINIAPP_PAYWALL_INAPP", True)


def _create(pool, user, **body):
    request = miniapp_pay.MiniAppYookassaCreateRequest(**{"generations": 10, "amount_rubles": 299, **body})
    return miniapp_pay.create_embedded_payment(request, miniapp_user=user, is_preview=False, pool=pool)


def _fake_create(created):
    async def create(amount_rubles, description, generations, **kwargs):
        created.append(kwargs)
        n = len(created)
        return {"yookassa_id": f"771000{n}", "confirmation_url": "https://pay.test/abc", "status": "pending",
                "order_id": f"{TEST_ORDER}{n}", "provider": "tbank"}
    return create


# ── the API client: TLS and order ids ──────────────────────────────

def test_the_client_trusts_the_russian_root_only_for_itself():
    context = tbank_acq.ssl_context()
    names = {dict(x[0] for x in cert["subject"]).get("commonName") for cert in context.get_ca_certs()}
    assert {"Russian Trusted Root CA", "Russian Trusted Sub CA"} <= names
    assert tbank_acq.ssl_context() is context  # built once


def test_test_terminal_orders_are_marked_and_real_ones_are_not(monkeypatch):
    assert tbank_acq.new_order_id().startswith("gl") and not tbank_acq.is_test_order(tbank_acq.new_order_id())
    monkeypatch.setattr(settings, "TBANK_TEST_MODE", True)
    order = tbank_acq.new_order_id()
    assert order.startswith("gltest") and tbank_acq.is_test_order(order) and len(order) <= 50


# ── who may pay in test mode ───────────────────────────────────────

def test_in_test_mode_only_the_listed_accounts_pay_and_always_through_tbank(test_mode, monkeypatch):
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", "7001, 7002")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "yookassa")
    assert payment_gateway.choose_provider(7001) == payment_gateway.choose_provider(7002) == "tbank"
    with pytest.raises(HTTPException) as denied:
        payment_gateway.choose_provider(1)
    assert denied.value.status_code == 403
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", "")
    with pytest.raises(HTTPException):
        payment_gateway.choose_provider(7001)


@pytest.mark.asyncio
async def test_a_stranger_cannot_start_a_test_payment(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    stranger = await _tg_user(clean_db, 1003)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", "999999")
    created = []
    monkeypatch.setattr(tbank_acq, "create_payment", _fake_create(created))
    with pytest.raises(HTTPException) as denied:
        await _create(clean_db, stranger, method="redirect", embed=True)
    assert denied.value.status_code == 403 and not created
    assert await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


# ── checkout: the form inside the app ──────────────────────────────

@pytest.mark.asyncio
async def test_the_owner_gets_an_embedded_form_and_our_return_pages(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    owner = await _tg_user(clean_db, 1004)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", str(owner["user"]["id"]))
    created = []
    monkeypatch.setattr(tbank_acq, "create_payment", _fake_create(created))
    track = AsyncMock()
    monkeypatch.setattr(miniapp_pay.analytics, "track", track)

    result = await _create(clean_db, owner, method="redirect", flow="web", embed=True)
    assert (result["provider"], result["embed"], result["confirmation_url"]) == ("tbank", True, "https://pay.test/abc")
    assert created[0]["return_url"] == "https://app.test/res-api/v1/miniapp/pay-return?p=tg&ok=1"
    assert created[0]["fail_url"] == "https://app.test/res-api/v1/miniapp/pay-return?p=tg&ok=0"
    # the funnel does not hear about a test payment
    assert "yookassa_payment_created" not in [call.args[0] for call in track.await_args_list]
    row = await row_of(clean_db, "7710001")
    assert (row["provider"], row["status"], row["provider_order_id"]) == ("tbank", "pending", TEST_ORDER + "1")

    # a client that does not ask for the frame (or a flag that is off) gets the plain redirect
    assert (await _create(clean_db, owner, method="redirect", embed=False))["embed"] is False
    monkeypatch.setattr(settings, "TBANK_EMBED", False)
    assert (await _create(clean_db, owner, method="redirect", embed=True))["embed"] is False


@pytest.mark.asyncio
async def test_top_level_form_returns_to_the_page_the_buyer_left(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    owner = await _tg_user(clean_db, 1017)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", str(owner["user"]["id"]))
    created = []
    monkeypatch.setattr(tbank_acq, "create_payment", _fake_create(created))
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())
    page = "https://app.your-domain.example/max-res-a/"
    monkeypatch.setattr(settings, "TBANK_TOP_LEVEL", True)  # off by default since the buttons

    # the app sends the page it leaves from: the form opens in the same WebView, the frame is not offered even if asked
    result = await _create(clean_db, owner, method="redirect", flow="web", embed=True, return_to=page)
    assert (result["top_level"], result["embed"]) == (True, False)
    assert created[0]["return_url"].endswith("/pay-return?p=tg&to=https%3A%2F%2Fapp.your-domain.example%2Fmax-res-a%2F&ok=1")
    assert created[0]["fail_url"].endswith("&to=https%3A%2F%2Fapp.your-domain.example%2Fmax-res-a%2F&ok=0")

    # a foreign address, a query or no address at all: no return page, the old behaviour
    for bad in ("https://evil.example/app/", "http://app.your-domain.example/max/", "https://app.your-domain.example/<script>"):
        result = await _create(clean_db, owner, method="redirect", flow="web", embed=True, return_to=bad)
        assert (result["top_level"], result["embed"]) == (False, True)
        assert "&to=" not in created[-1]["return_url"]

    # switched off: back to the frame
    await clean_db.execute("UPDATE yookassa_payments SET status = 'canceled'")  # the 5-unpaid limit is not under test
    monkeypatch.setattr(settings, "TBANK_TOP_LEVEL", False)
    result = await _create(clean_db, owner, method="redirect", flow="web", embed=True, return_to=page)
    assert (result["top_level"], result["embed"]) == (False, True)


def test_only_our_own_origins_are_returned_to(monkeypatch):
    monkeypatch.setattr(miniapp_pay.edge, "EDGE_MEDIA_URL", "https://edge.your-domain.example/s3")
    assert miniapp_pay.safe_return_to("https://app.your-domain.example/app/") == "https://app.your-domain.example/app/"
    assert miniapp_pay.safe_return_to("https://edge.your-domain.example/max-next/") == "https://edge.your-domain.example/max-next/"
    assert miniapp_pay.safe_return_to("https://app.your-domain.example") == "https://app.your-domain.example/"
    for bad in (None, "", "//evil.example/", "https://app.your-domain.example.evil.example/", "https://evil.example/x",
                "https://app.your-domain.example/a#b", "https://app.your-domain.example/a?b=1", "javascript:alert(1)",
                "https://app.your-domain.example/a b"):
        assert miniapp_pay.safe_return_to(bad) is None


@pytest.mark.asyncio
async def test_the_package_list_says_where_the_payment_goes_and_that_it_is_a_test(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    owner, stranger = await _tg_user(clean_db, 1005), await _tg_user(clean_db, 1006)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", str(owner["user"]["id"]))
    packages = lambda user: miniapp_pay.get_packages(SimpleNamespace(headers={}), miniapp_user=user, pool=clean_db)  # noqa: E731
    mine, other = await packages(owner), await packages(stranger)
    assert (mine["provider"], mine["test_mode"]) == ("tbank", True)
    assert (other["provider"], other["test_mode"]) == ("yookassa", False)  # the sheet shows; the payment itself is refused


# ── settlement: credited, but never as money ───────────────────────

@pytest.mark.asyncio
async def test_a_test_terminal_payment_credits_energy_but_is_not_revenue(client, clean_db, monkeypatch):
    track = AsyncMock()
    monkeypatch.setattr(payments_route.analytics, "track", track)
    user = await seed_user(clean_db, 90101)
    await clean_db.execute("UPDATE users SET has_purchased = FALSE WHERE id = $1", user)
    await seed_payment(clean_db, user, 90101, pid="7710001", order=TEST_ORDER)

    answer = await client.post(NOTIFY_URL, json=notification(pid="7710001", order=TEST_ORDER))
    assert (answer.status_code, answer.text) == (200, "OK")

    assert await balance_of(clean_db, user) == 13                      # the same credit as a real payment
    row = await row_of(clean_db, "7710001")
    assert row["status"] == "test_owner" and row["completed_at"] is not None
    # everything that reads revenue filters on status = 'succeeded' (Pulse, 💰 notice, NPD receipts, ad guard, H-metrics)
    assert await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments WHERE status = 'succeeded'") == 0
    assert await clean_db.fetchval("SELECT has_purchased FROM users WHERE id = $1", user) is False
    assert await clean_db.fetchval("SELECT comment FROM payments WHERE user_id = $1", user) == "T-Bank TEST: 299₽"
    assert await clean_db.fetchval("SELECT COUNT(*) FROM metrika_conversions") == 0
    assert await clean_db.fetchval("SELECT COUNT(*) FROM vk_conversions") == 0
    assert track.await_count == 0                                       # no payment_completed / yookassa_payment_succeeded

    # the notification repeats: still once
    await client.post(NOTIFY_URL, json=notification(pid="7710001", order=TEST_ORDER))
    assert await balance_of(clean_db, user) == 13


@pytest.mark.asyncio
async def test_the_app_sees_a_test_payment_as_paid(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    owner = await _tg_user(clean_db, 1007)
    uid = owner["user"]["id"]
    await seed_payment(clean_db, uid, 1007, pid="7710002", order=TEST_ORDER + "x")
    monkeypatch.setattr(tbank_acq, "get_payment_status", AsyncMock(return_value={
        "yookassa_id": "7710002", "order_id": TEST_ORDER + "x", "status": "succeeded", "paid": True,
        "amount_value": 299, "currency": "RUB", "metadata": {}, "payment_method": None}))

    class FakeRequest:
        class app:
            class state:
                notifications_client = None

    answer = await miniapp_pay.get_payment_status("7710002", FakeRequest(), miniapp_user=owner, pool=clean_db)
    assert answer["status"] == "succeeded" and answer["credited_generations"] == 10
    assert (await row_of(clean_db, "7710002"))["status"] == "test_owner"
    again = await miniapp_pay.get_payment_status("7710002", FakeRequest(), miniapp_user=owner, pool=clean_db)
    assert again["status"] == "succeeded" and again["balance"] == answer["balance"]


@pytest.mark.asyncio
async def test_a_real_terminal_payment_is_still_revenue(client, clean_db):
    user = await seed_user(clean_db, 90102)
    await seed_payment(clean_db, user, 90102, pid="7710003", order="glreal" + "cd" * 12)
    await client.post(NOTIFY_URL, json=notification(pid="7710003", order="glreal" + "cd" * 12))
    assert (await row_of(clean_db, "7710003"))["status"] == "succeeded"
    assert await clean_db.fetchval("SELECT has_purchased FROM users WHERE id = $1", user) is True


# ── the page T-Bank sends the buyer to ─────────────────────────────

@pytest.mark.asyncio
async def test_the_return_page_tells_the_app_and_offers_a_way_back(monkeypatch):
    monkeypatch.setattr(settings, "BOT_USERNAME", "your_bot")
    monkeypatch.setattr(settings, "MAX_BOT_USERNAME", "your_max_bot")
    good = (await miniapp_pay.pay_return(p="tg", ok="1")).body.decode()
    bad = (await miniapp_pay.pay_return(p="max", ok="0")).body.decode()
    assert "app-pay-return" in good and "var ok = true" in good and "https://t.me/your_bot?startapp=pay_return" in good
    assert "var ok = false" in bad and "https://max.ru/your_max_bot?startapp=pay_return" in bad
    assert "var to = null;" in good
    # at the top level the page returns to the Mini App page it was told about (only our own origins)
    back = (await miniapp_pay.pay_return(p="tg", ok="1", to="https://app.your-domain.example/app/")).body.decode()
    assert 'var to = "https://app.your-domain.example/app/";' in back and "?pay=return&ok=" in back
    foreign = (await miniapp_pay.pay_return(p="tg", ok="1", to="https://evil.example/x")).body.decode()
    assert "var to = null;" in foreign and "evil.example" not in foreign


@pytest.mark.asyncio
async def test_a_retry_on_the_same_form_after_a_refusal_is_credited_by_the_poll(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    owner = await _tg_user(clean_db, 1008)
    uid = owner["user"]["id"]
    order = TEST_ORDER + "r"
    await seed_payment(clean_db, uid, 1008, pid="7710003", order=order)
    state = {"status": "canceled", "paid": False}

    async def remote(payment):
        return {"yookassa_id": "7710003", "order_id": order, "status": state["status"], "paid": state["paid"],
                "amount_value": 299, "currency": "RUB", "metadata": {}, "payment_method": None}

    monkeypatch.setattr(payment_gateway, "get_payment_status", remote)

    class FakeRequest:
        class app:
            class state:
                notifications_client = None

    poll = lambda: miniapp_pay.get_payment_status("7710003", FakeRequest(), miniapp_user=owner, pool=clean_db)  # noqa: E731
    assert (await poll())["status"] == "canceled"                       # REJECTED: closed
    before = await balance_of(clean_db, uid)
    state.update(status="succeeded", paid=True)                         # the buyer paid with another card on the same form
    answer = await poll()
    assert answer["status"] == "succeeded" and await balance_of(clean_db, uid) == before + 10
    assert (await row_of(clean_db, "7710003"))["status"] == "test_owner"
    assert (await poll())["balance"] == before + 10                     # once

    # an old refusal (the form is long gone) is not read again
    await seed_payment(clean_db, uid, 1008, pid="7710004", order=order + "2", status="canceled")
    await clean_db.execute("UPDATE yookassa_payments SET completed_at = now() - interval '2 hours' WHERE yookassa_id = '7710004'")
    probe = AsyncMock(side_effect=AssertionError("must not be read"))
    monkeypatch.setattr(payment_gateway, "get_payment_status", probe)
    old = await miniapp_pay.get_payment_status("7710004", FakeRequest(), miniapp_user=owner, pool=clean_db)
    assert old["status"] == "canceled" and probe.await_count == 0
