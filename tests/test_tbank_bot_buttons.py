"""SberPay · SBP · T-Pay · card in the chat of the bots (TG, MAX) for a T-Bank payment: the answer of the create call,
the signed key, the pages ``pay-bot`` and ``pay-choose``. The bank's answers are those seen on the terminal on 10.11."""
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.api.routes import max as max_route
from app.api.routes import payments as payments_route
from app.core.config import settings
from app.db import yookassa_repo
from app.services import bot_pay, tbank_acq, tbank_methods
from tests.conftest import auth_headers
from tests.test_miniapp_pay import _seed_tiers, _tg_user  # noqa: F401
from tests.test_tbank_acquiring import fake_tbank_create, row_of, tbank_settings  # noqa: F401

DEEPLINK = "sberpay://invoicing/v2?bankInvoiceId=01a1c4b462b359ab199781ed7f7db029&operationType=Web2App"
ALL_WALLETS = "sberbank,sbp,tinkoff_bank"
TG_BODY = {"telegram_id": 1001, "generations": 10, "amount_rubles": 299}


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    tbank_methods.reset()
    monkeypatch.setattr(settings, "TBANK_BUTTONS", True)
    monkeypatch.setattr(settings, "TBANK_BOT_BUTTONS", True)
    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", ALL_WALLETS)  # the wallets are open without asking the bank
    monkeypatch.setattr(settings, "TBANK_RETURN_URL", "https://app.test/api/v1/miniapp/pay-return")
    yield
    tbank_methods.reset()


@pytest.fixture
def bank(monkeypatch):
    """A T-Bank that makes payments (``created`` keeps the Init calls) and gives a link for every wallet."""
    created = []
    monkeypatch.setattr(tbank_acq, "create_payment", fake_tbank_create(created))
    monkeypatch.setattr(payments_route.analytics, "track", AsyncMock())
    monkeypatch.setattr(max_route.analytics, "track", AsyncMock())
    links = {"sberbank": DEEPLINK, "sbp": "https://qr.nspk.ru/AD2010", "tinkoff_bank": "https://tpay.test/check/?id=1"}

    async def wallet_link(method, payment_id, **_):
        return links[method]

    monkeypatch.setattr(tbank_acq, "wallet_link", AsyncMock(side_effect=wallet_link))
    return created


async def _tg_buyer(pool, monkeypatch, telegram_id=1001, *, tbank=True):
    await _seed_tiers(pool)
    user = await _tg_user(pool, telegram_id)
    if tbank:
        monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", str(user["user"]["id"]))
    return user


async def _tg_create(client, **body):
    return (await client.post("/api/v1/payments/yookassa/create", json={**TG_BODY, **body}, headers=auth_headers())).json()


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def _key(url: str) -> str:
    return parse_qs(urlsplit(url).query)["k"][0]


# ── the key ────────────────────────────────────────────────────────

def test_a_key_carries_the_payment_the_buyer_and_the_platform_and_lives_longer_than_a_day():
    key = bot_pay.issue(payment_id="771001", user_id=7, platform="max", now=1_000_000)
    assert bot_pay.verify(key, now=1_000_000 + 24 * 3600) == bot_pay.BotLink("771001", 7, "max", 1_000_000 + bot_pay.TTL_SECONDS)
    assert bot_pay.TTL_SECONDS >= 24 * 3600
    assert bot_pay.verify(key, now=1_000_000 + bot_pay.TTL_SECONDS + 1) is None


def test_a_forged_key_is_refused_whatever_is_changed():
    key = bot_pay.issue(payment_id="771001", user_id=7, platform="tg")
    prefix, body, signature = key.split(".")
    other = bot_pay.issue(payment_id="771002", user_id=7, platform="tg").split(".")[1]
    for bad in (None, "", "x", key + "0", f"{prefix}.{other}.{signature}", f"gp1.{body}.{signature}", f"{prefix}.{body}.{'0' * 64}",
                f"{prefix}.{body}"):
        assert bot_pay.verify(bad) is None
    # a ticket of the Mini App is not a key (and the other way round: the HMAC labels differ)
    from app.services import pay_ticket
    ticket, _ = pay_ticket.issue(user_id=7, platform="max", external_id="1", generations=10, amount_rubles=299, context="menu", source=None)
    assert bot_pay.verify(ticket) is None and pay_ticket.verify(key) is None


# ── the create call of the bots ────────────────────────────────────

@pytest.mark.asyncio
async def test_the_tg_bot_gets_the_four_buttons_in_the_order_sberpay_sbp_tpay_card(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)

    answer = await _tg_create(client)

    buttons = answer["pay_buttons"]
    assert [(b["method"], b["text"]) for b in buttons] == [
        ("sberpay", "SberPay"), ("sbp", "СБП"), ("tpay", "T-Pay"), ("card", "Картой")]
    key = _key(buttons[0]["url"])
    for button in buttons:
        query = parse_qs(urlsplit(button["url"]).query)
        assert urlsplit(button["url"]).path == "/api/v1/miniapp/pay-bot"
        assert (query["k"], query["m"], query["p"]) == ([key], [button["method"]], ["tg"])
    link = bot_pay.verify(key)
    assert (link.payment_id, link.platform) == (answer["yookassa_id"], "tg")
    # an old bot opens ``confirmation_url``: the page with the same choice; the row keeps the bank's own form
    assert answer["confirmation_url"].startswith("https://app.test/api/v1/miniapp/pay-choose?k=")
    assert _key(answer["confirmation_url"]) == key
    row = await row_of(clean_db, answer["yookassa_id"])
    assert (row["provider"], row["confirmation_url"]) == ("tbank", "https://securepay.test/pay/1")
    # the payment is made as before (the bot polls it), and Init carries the flags that open the SberPay / T-Pay links
    assert len(bank) == 1
    assert bank[0]["device_data"] == {"SberPayWeb": True, "TinkoffPayWeb": True, "Device": "Mobile"}


@pytest.mark.asyncio
async def test_only_the_ways_the_terminal_has_get_a_button_and_the_card_stays_last(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", "tinkoff_bank,sberbank")

    answer = await _tg_create(client)

    assert [b["method"] for b in answer["pay_buttons"]] == ["sberpay", "tpay", "card"]


@pytest.mark.asyncio
async def test_a_terminal_without_wallets_leaves_the_single_button_as_before(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", "")
    for method in tbank_acq.WALLET_METHODS:
        tbank_methods._state[method] = (time.monotonic(), False)  # the probe found them closed

    answer = await _tg_create(client)

    assert "pay_buttons" not in answer and answer["confirmation_url"] == "https://securepay.test/pay/1"


@pytest.mark.asyncio
async def test_the_buttons_flag_off_or_yookassa_changes_nothing(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    yk_calls = []

    async def yk_create(**kwargs):
        yk_calls.append(kwargs)
        return {"yookassa_id": f"yk-{len(yk_calls)}", "confirmation_url": "https://yk/pay", "status": "pending"}

    monkeypatch.setattr(payments_route.yookassa_service, "create_sbp_payment", yk_create)

    monkeypatch.setattr(settings, "TBANK_BUTTONS", False)
    off = await _tg_create(client)
    assert "pay_buttons" not in off and off["confirmation_url"] == "https://securepay.test/pay/1"
    assert "device_data" not in bank[0] or not bank[0]["device_data"]  # no wallet flags without the buttons

    monkeypatch.setattr(settings, "TBANK_BUTTONS", True)
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", "")  # not a T-Bank buyer
    plain = await _tg_create(client)
    assert plain == {"yookassa_id": "yk-1", "confirmation_url": "https://yk/pay", "generations": 10, "amount_rubles": 299,
                     "bonus_generations": 0}
    assert "device_data" not in yk_calls[0]
    assert len(bank) == 1  # T-Bank was not asked for the YooKassa buyer


@pytest.mark.asyncio
async def test_a_bank_that_failed_at_checkout_is_an_error_and_yookassa_is_not_asked(client, clean_db, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)

    async def broken(*_, **__):
        raise tbank_acq.TBankError("down", unknown=False)

    async def yk_create(**_):
        raise AssertionError("YooKassa must not be called when T-Bank fails")

    monkeypatch.setattr(tbank_acq, "create_payment", broken)
    monkeypatch.setattr(payments_route.yookassa_service, "create_sbp_payment", yk_create)
    monkeypatch.setattr(payments_route.analytics, "track", AsyncMock())

    try:
        response = await client.post("/api/v1/payments/yookassa/create", json=TG_BODY, headers=auth_headers())
    except tbank_acq.TBankError:
        pass  # the test client re-raises the server error
    else:
        assert response.status_code >= 500
    assert await clean_db.fetchval("SELECT count(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_the_max_bot_gets_the_same_buttons_and_a_repeated_call_the_same_payment(client, clean_db, bank, monkeypatch):
    await _seed_tiers(clean_db)
    from app.db import users_repo
    created = await users_repo.get_or_create_user_by_identity(clean_db, "max", "559100")
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", str(created["user"]["id"]))
    monkeypatch.setattr(settings, "MAX_BOT_USERNAME", "your_max_bot")
    body = {"external_id": "559100", "generations": 10, "amount_rubles": 299, "context": "menu", "operation_key": "k-1"}

    first = (await client.post("/api/v1/max/payments/yookassa/create", json=body, headers=auth_headers())).json()
    again = (await client.post("/api/v1/max/payments/yookassa/create", json=body, headers=auth_headers())).json()

    assert [b["method"] for b in first["pay_buttons"]] == ["sberpay", "sbp", "tpay", "card"]
    assert all(parse_qs(urlsplit(b["url"]).query)["p"] == ["max"] for b in first["pay_buttons"])
    assert bot_pay.verify(_key(first["pay_buttons"][0]["url"])).platform == "max"
    assert again["yookassa_id"] == first["yookassa_id"] and len(bank) == 1
    assert [b["method"] for b in again["pay_buttons"]] == ["sberpay", "sbp", "tpay", "card"]
    assert again["confirmation_url"].startswith("https://app.test/api/v1/miniapp/pay-choose?k=")
    assert (await row_of(clean_db, first["yookassa_id"]))["confirmation_url"] == "https://securepay.test/pay/1"
    assert bank[0]["device_data"]["SberPayWeb"] is True


# ── pay-bot ────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("way, bank_method, target", [
    ("sbp", "sbp", "https://qr.nspk.ru/AD2010"),
    ("tpay", "tinkoff_bank", "https://tpay.test/check/?id=1"),
])
async def test_a_wallet_button_redirects_to_the_link_the_bank_gives_for_the_payment_already_made(
    client, clean_db, bank, monkeypatch, way, bank_method, target,
):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    button = next(b for b in answer["pay_buttons"] if b["method"] == way)

    page = await client.get(_path(button["url"]))

    assert (page.status_code, page.headers["location"]) == (302, target)
    assert tbank_acq.wallet_link.await_args.args == (bank_method, answer["yookassa_id"])
    assert len(bank) == 1  # nothing is created by a tap: the payment the bot polls is the one that is paid
    assert await clean_db.fetchval("SELECT count(*) FROM yookassa_payments") == 1
    # a second tap (or a reload) asks the bank for the link again and lands on the same place
    assert (await client.get(_path(button["url"]))).headers["location"] == target


@pytest.mark.asyncio
async def test_sberpay_goes_through_our_page_that_opens_the_bank_app_and_the_card_to_the_bank_form(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    by_way = {b["method"]: b["url"] for b in answer["pay_buttons"]}

    sber = await client.get(_path(by_way["sberpay"]))
    card = await client.get(_path(by_way["card"]))

    wrapped = urlsplit(sber.headers["location"])
    assert sber.status_code == 302 and wrapped.scheme == "https" and wrapped.path.endswith("/pay-open")
    assert parse_qs(wrapped.query) == {"p": ["tg"], "u": [DEEPLINK]}
    assert (card.status_code, card.headers["location"]) == (302, "https://securepay.test/pay/1")
    assert tbank_acq.wallet_link.await_count == 1  # the card button does not ask the bank for a wallet


@pytest.mark.asyncio
async def test_a_max_button_wraps_the_sberpay_link_for_max(client, clean_db, bank, monkeypatch):
    await _seed_tiers(clean_db)
    from app.db import users_repo
    created = await users_repo.get_or_create_user_by_identity(clean_db, "max", "559101")
    monkeypatch.setattr(settings, "TBANK_FORCE_USER_IDS", str(created["user"]["id"]))
    body = {"external_id": "559101", "generations": 10, "amount_rubles": 299, "context": "menu"}
    answer = (await client.post("/api/v1/max/payments/yookassa/create", json=body, headers=auth_headers())).json()

    sber = await client.get(_path(next(b["url"] for b in answer["pay_buttons"] if b["method"] == "sberpay")))

    assert parse_qs(urlsplit(sber.headers["location"]).query)["p"] == ["max"]


async def _pages(client, button_url):
    """What a buyer sees for a button: (status, text of the page or the redirect)."""
    page = await client.get(_path(button_url))
    return page.status_code, page.headers.get("location") or page.text


@pytest.mark.asyncio
async def test_a_forged_expired_or_foreign_key_opens_nothing(client, clean_db, bank, monkeypatch):
    user = await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    url = answer["pay_buttons"][0]["url"]
    other = await _tg_user(clean_db, 2002)

    key = _key(url)
    status, _ = await _pages(client, url.replace(key, key[:-2] + ("00" if key[-2:] != "00" else "11")))
    assert status == 410

    expired = bot_pay.issue(payment_id=answer["yookassa_id"], user_id=user["user"]["id"], platform="tg", now=1_000)
    assert (await client.get(f"/api/v1/miniapp/pay-bot?k={expired}&m=sbp&p=tg")).status_code == 410

    # a key signed for another buyer cannot open this buyer's payment (the page says «not found», not whose it is)
    foreign = bot_pay.issue(payment_id=answer["yookassa_id"], user_id=other["user"]["id"], platform="tg")
    page = await client.get(f"/api/v1/miniapp/pay-bot?k={foreign}&m=sbp&p=tg")
    assert page.status_code == 404 and "location" not in page.headers
    unknown = bot_pay.issue(payment_id="nobody", user_id=user["user"]["id"], platform="tg")
    assert (await client.get(f"/api/v1/miniapp/pay-bot?k={unknown}&m=sbp&p=tg")).status_code == 404
    assert tbank_acq.wallet_link.await_count == 0


@pytest.mark.asyncio
async def test_a_yookassa_payment_is_not_opened_by_a_bot_key(client, clean_db, bank, monkeypatch):
    user = await _tg_buyer(clean_db, monkeypatch, tbank=False)
    await yookassa_repo.create_payment(
        pool=clean_db, user_id=user["user"]["id"], telegram_id=1001, yookassa_id="yk-1", amount_rubles=299, generations=10,
        confirmation_url="https://yk/pay",
    )
    key = bot_pay.issue(payment_id="yk-1", user_id=user["user"]["id"], platform="tg")

    page = await client.get(f"/api/v1/miniapp/pay-bot?k={key}&m=card&p=tg")

    assert page.status_code == 404 and "location" not in page.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code, text", [
    ("succeeded", 200, "уже прошла"),
    ("canceled", 410, "закрыт"),
])
async def test_a_paid_or_closed_payment_is_a_page_with_a_way_back_to_the_chat(client, clean_db, bank, monkeypatch, status, code, text):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    await clean_db.execute("UPDATE yookassa_payments SET status = $2 WHERE yookassa_id = $1", answer["yookassa_id"], status)

    for button in (answer["pay_buttons"][0], answer["pay_buttons"][-1]):
        page = await client.get(_path(button["url"]))
        assert page.status_code == code and "location" not in page.headers
        assert text in page.text and f"https://t.me/{settings.BOT_USERNAME}" in page.text
    assert tbank_acq.wallet_link.await_count == 0
    assert (await client.get(_path(answer["confirmation_url"]))).status_code == code


@pytest.mark.asyncio
async def test_a_bad_way_is_refused_and_a_bank_that_closed_a_wallet_hides_its_button(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    key = _key(answer["confirmation_url"])
    assert (await client.get(f"/api/v1/miniapp/pay-bot?k={key}&m=paypal&p=tg")).status_code == 400

    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", "")  # from here the probe's memory decides, and it says «all open»
    for method in tbank_acq.WALLET_METHODS:
        tbank_methods._state[method] = (time.monotonic(), True)

    tbank_acq.wallet_link.side_effect = tbank_acq.TBankError("closed", code="3001")
    closed = await client.get(f"/api/v1/miniapp/pay-bot?k={key}&m=sbp&p=tg")
    assert closed.status_code == 409 and "location" not in closed.headers
    assert f"/pay-choose?k={key}" in closed.text.replace("&amp;", "&")  # the way back is the choice
    assert "sbp" not in await bot_pay.open_wallet_ways()

    # a hiccup of the bank is not a closed way: the button stays
    tbank_acq.wallet_link.side_effect = tbank_acq.TBankError("timeout", unknown=True)
    hiccup = await client.get(f"/api/v1/miniapp/pay-bot?k={key}&m=tpay&p=tg")
    assert hiccup.status_code == 502 and "tpay" in await bot_pay.open_wallet_ways()


@pytest.mark.asyncio
async def test_with_the_buttons_switched_off_a_wallet_link_is_not_asked_but_the_card_still_opens(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    monkeypatch.setattr(settings, "TBANK_BUTTONS", False)

    wallet = await client.get(_path(answer["pay_buttons"][1]["url"]))
    card = await client.get(_path(answer["pay_buttons"][-1]["url"]))

    assert wallet.status_code == 409 and tbank_acq.wallet_link.await_count == 0
    assert card.status_code == 302


# ── pay-choose ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_choice_page_shows_the_price_and_a_link_for_every_way(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)

    page = await client.get(_path(answer["confirmation_url"]))

    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    html = page.text.replace("&amp;", "&")
    assert "299 ₽" in html and "10 ⚡ энергии" in html
    hrefs = [button["url"] for button in answer["pay_buttons"]]
    positions = [html.index(href) for href in hrefs]
    assert positions == sorted(positions)  # SberPay, SBP, T-Pay, card
    assert html.count("data:image/svg+xml;base64,") == 3  # the logos of the three wallets travel inside the page
    assert tbank_acq.wallet_link.await_count == 0  # looking at the choice asks the bank for nothing


@pytest.mark.asyncio
async def test_the_choice_page_hides_a_closed_wallet_and_refuses_a_bad_key(client, clean_db, bank, monkeypatch):
    await _tg_buyer(clean_db, monkeypatch)
    answer = await _tg_create(client)
    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", "sbp")

    html = (await client.get(_path(answer["confirmation_url"]))).text.replace("&amp;", "&")

    assert "m=sbp" in html and "m=card" in html and "m=sberpay" not in html and "m=tpay" not in html
    assert (await client.get("/api/v1/miniapp/pay-choose?k=gb1.x.y&p=max")).status_code == 410


@pytest.mark.asyncio
async def test_bot_buttons_off_keeps_the_bank_form_but_still_sends_the_wallet_flags(client, clean_db, monkeypatch, bank):
    await _tg_buyer(clean_db, monkeypatch)
    monkeypatch.setattr(settings, "TBANK_BOT_BUTTONS", False)
    answer = await _tg_create(client)
    assert "pay_buttons" not in answer and answer["confirmation_url"] == "https://securepay.test/pay/1"
    assert bank[0]["device_data"] == bot_pay.BOT_DEVICE_DATA
