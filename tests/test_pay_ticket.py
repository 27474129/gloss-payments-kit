"""The ticket of the wallet buttons: signed and short-lived, creates nothing; ``pay-go`` makes the payment from it
(idempotent per ticket and way) and redirects to the bank's link; the sheet follows the ticket's payments."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.routes import miniapp_pay
from app.core.config import settings
from app.services import pay_ticket, tbank_acq
from tests.test_miniapp_pay import _max_user, _seed_tiers, _tg_user  # noqa: F401
from tests.test_tbank_acquiring import tbank_settings  # noqa: F401
from tests.test_tbank_test_mode import _fake_create, test_mode  # noqa: F401

IPHONE = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}
SBP_LINK = "https://qr.nspk.ru/AS1000"
TPAY_LINK = "https://tpay.tbank-online.com/check/abc"
DEEPLINK = "sberpay://invoicing/v2?bankInvoiceId=01a1c4b462b359ab199781ed7f7db029&operationType=Web2App"
LINKS = {"sbp": SBP_LINK, "tinkoff_bank": TPAY_LINK, "sberbank": DEEPLINK}


def _issue(**overrides):
    values = dict(user_id=7, platform="max", external_id="204127618", generations=10, amount_rubles=299, context="menu", source="header")
    return pay_ticket.issue(**{**values, **overrides})


# ── the ticket itself ──────────────────────────────────────────────

def test_a_ticket_carries_what_was_signed_and_expires_in_ten_minutes():
    token, expires_at = _issue(now=1_000_000)
    assert expires_at == 1_000_000 + 600
    ticket = pay_ticket.verify(token, now=1_000_100)
    assert (ticket.user_id, ticket.platform, ticket.external_id, ticket.generations, ticket.amount_rubles, ticket.context) == (
        7, "max", "204127618", 10, 299, "menu")
    assert pay_ticket.verify(token, now=1_000_600) is not None
    assert pay_ticket.verify(token, now=1_000_601) is None
    # the status of a payment is read much later than the ticket may start one
    assert pay_ticket.verify(token, now=1_000_601, leeway=pay_ticket.STATUS_LEEWAY_SECONDS) is not None


def test_a_forged_ticket_is_refused_whatever_is_changed():
    token, _ = _issue()
    prefix, body, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))

    def reencode(**changes):
        cheap = {**payload, **changes}
        return base64.urlsafe_b64encode(json.dumps(cheap, separators=(",", ":")).encode()).decode().rstrip("=")

    assert pay_ticket.verify(token) is not None
    assert pay_ticket.verify(f"{prefix}.{reencode(a=1)}.{signature}") is None      # a lower price
    assert pay_ticket.verify(f"{prefix}.{reencode(u=8)}.{signature}") is None      # somebody else's account
    assert pay_ticket.verify(f"{prefix}.{reencode(e=payload['e'] + 10**6)}.{signature}") is None  # a longer life
    assert pay_ticket.verify(f"{prefix}.{body}.{'0' * 64}") is None
    for junk in ("", None, "gp1", "gp1.a.b", "x.y.z", token + ".extra", token[:-1]):
        assert pay_ticket.verify(junk) is None


def test_the_signature_follows_the_secret(monkeypatch):
    token, _ = _issue()
    monkeypatch.setattr(settings, "API_SECRET_TOKEN", "another-secret")
    assert pay_ticket.verify(token) is None


# ── the endpoints ──────────────────────────────────────────────────

@pytest.fixture
def bank(clean_db, test_mode, monkeypatch):
    """T-Bank (test terminal) with the owner allowed; ``Init`` hands out ids 7710001…, links per way."""
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())
    created, canceled, link_calls = [], [], []
    monkeypatch.setattr(tbank_acq, "create_payment", _fake_create(created))

    async def cancel(payment_id, *_, **__):
        canceled.append(payment_id)
        return {}

    async def wallet_link(method, payment_id, **_):
        link_calls.append((method, payment_id))
        return LINKS[method]

    monkeypatch.setattr(tbank_acq, "cancel_payment", cancel)
    monkeypatch.setattr(tbank_acq, "wallet_link", wallet_link)
    return SimpleNamespace(created=created, canceled=canceled, links=link_calls)


async def _owner(pool, monkeypatch, make_user=_tg_user, *args):
    await _seed_tiers(pool)
    owner = await make_user(pool, *args)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", str(owner["user"]["id"]))
    return owner


def _ticket(pool, user, **body):
    request = miniapp_pay.PayTicketRequest(**{"generations": 10, "amount_rubles": 299, "context": "menu", "source": "header", **body})
    return miniapp_pay.issue_pay_ticket(request, miniapp_user=user, is_preview=False, pool=pool)


def _go(pool, ticket, way, headers=IPHONE, p="tg"):
    return miniapp_pay.pay_go(SimpleNamespace(headers=headers), t=ticket, m=way, p=p, pool=pool)


async def _status(pool, user, ticket):
    return await miniapp_pay.get_ticket_status(ticket, SimpleNamespace(), miniapp_user=user, pool=pool)


@pytest.mark.asyncio
async def test_issuing_a_ticket_checks_the_price_and_creates_nothing(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    answer = await _ticket(clean_db, owner)
    ticket = pay_ticket.verify(answer["ticket"])
    assert (ticket.user_id, ticket.generations, ticket.amount_rubles, ticket.platform) == (owner["user"]["id"], 10, 299, "telegram")
    assert answer["expires_at"].endswith("+00:00")
    assert bank.created == [] and await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0

    with pytest.raises(HTTPException) as price:
        await _ticket(clean_db, owner, amount_rubles=1)
    assert price.value.status_code == 400
    with pytest.raises(HTTPException) as preview:
        await miniapp_pay.issue_pay_ticket(
            miniapp_pay.PayTicketRequest(generations=10, amount_rubles=299), miniapp_user=owner, is_preview=True, pool=clean_db,
        )
    assert preview.value.status_code == 403


@pytest.mark.asyncio
async def test_a_buyer_without_the_buttons_gets_no_ticket(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    monkeypatch.setattr(settings, "TBANK_BUTTONS", False)
    with pytest.raises(HTTPException) as refused:
        await _ticket(clean_db, owner)
    assert refused.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("way, link, bank_method", [("sbp", SBP_LINK, "sbp"), ("tpay", TPAY_LINK, "tinkoff_bank")])
async def test_pay_go_makes_the_payment_and_redirects_to_the_bank_link(clean_db, bank, monkeypatch, way, link, bank_method):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]

    response = await _go(clean_db, ticket, way)

    assert (response.status_code, response.headers["location"]) == (302, link)
    assert bank.links == [(bank_method, "7710001")]
    row = await clean_db.fetchrow("SELECT * FROM yookassa_payments")
    assert (row["status"], row["amount_rubles"], row["generations"], row["provider"], row["user_id"]) == (
        "pending", 299, 10, "tbank", owner["user"]["id"])
    assert row["client_operation_key"] == pay_ticket.verify(ticket).operation_key(way)


@pytest.mark.asyncio
async def test_sberpay_goes_through_our_page_that_opens_the_bank_app(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch, _max_user, "770123")
    ticket = (await _ticket(clean_db, owner))["ticket"]
    response = await _go(clean_db, ticket, "sberpay", p="tg")  # the platform comes from the ticket, not the query
    location = response.headers["location"]
    assert response.status_code == 302 and location.startswith("https://") and "/pay-open?p=max&u=sberpay%3A%2F%2Finvoicing" in location
    assert bank.created[0]["device_data"] == {"Device": "Mobile", "SberPayWeb": True}


@pytest.mark.asyncio
async def test_a_desktop_browser_asks_the_bank_for_the_desktop_flow(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    await _go(clean_db, ticket, "tpay", headers={"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"})
    assert bank.created[0]["device_data"] == {"Device": "Desktop", "TinkoffPayWeb": True}


@pytest.mark.asyncio
async def test_a_second_tap_is_the_same_payment_and_another_way_is_another_one(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]

    first = await _go(clean_db, ticket, "sbp")
    again = await _go(clean_db, ticket, "sbp")
    assert first.headers["location"] == again.headers["location"] == SBP_LINK
    assert len(bank.created) == 1 and await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 1
    assert bank.links == [("sbp", "7710001"), ("sbp", "7710001")]  # the link is asked again for the same payment

    await _go(clean_db, ticket, "tpay")
    assert len(bank.created) == 2 and await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 2
    # a new ticket is a new payment
    await _go(clean_db, (await _ticket(clean_db, owner))["ticket"], "sbp")
    assert len(bank.created) == 3


@pytest.mark.asyncio
async def test_a_double_tap_at_once_leaves_one_payment_and_closes_the_twin(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    real = miniapp_pay.yookassa_repo.get_by_operation_key
    calls = {"n": 0}

    async def blind_first_lookup(pool, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(pool, **kwargs)

    await _go(clean_db, ticket, "sbp")  # the first request stored its payment …
    monkeypatch.setattr(miniapp_pay.yookassa_repo, "get_by_operation_key", blind_first_lookup)
    response = await _go(clean_db, ticket, "sbp")  # … the second did not see it yet and tried to create its own
    assert response.status_code == 302 and response.headers["location"] == SBP_LINK
    assert await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 1
    assert bank.canceled == ["7710002"]


@pytest.mark.asyncio
async def test_pay_go_refuses_bad_expired_and_stale_tickets_with_a_page_and_creates_nothing(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    forged = ticket[:-3] + ("aaa" if not ticket.endswith("aaa") else "bbb")

    for token, way, status in ((forged, "sbp", 410), (ticket, "bitcoin", 410), ("", "sbp", 410)):
        page = await _go(clean_db, token, way)
        assert page.status_code == status and "Вернуться в YOUR_BRAND" in page.body.decode()

    expired, _ = pay_ticket.issue(user_id=owner["user"]["id"], platform="telegram", external_id=1001, generations=10, amount_rubles=299,
                                  context="menu", source=None, now=1_000)
    assert (await _go(clean_db, expired, "sbp")).status_code == 410

    # the price list changed after the ticket was issued
    await clean_db.execute("UPDATE pricing SET value = REPLACE(value::text, '299', '399')::jsonb WHERE key = 'tiers'")
    page = await _go(clean_db, ticket, "sbp")
    assert page.status_code == 400 and "Цены обновились" in page.body.decode()
    assert bank.created == [] and await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_a_ticket_of_a_price_nobody_sells_cannot_be_made_even_with_the_secret(clean_db, bank, monkeypatch):
    """The server signs only what it checked, but ``pay-go`` checks again: a ticket signed for a wrong price is refused."""
    owner = await _owner(clean_db, monkeypatch)
    cheap, _ = pay_ticket.issue(user_id=owner["user"]["id"], platform="telegram", external_id=1001, generations=10, amount_rubles=1,
                                context="menu", source=None)
    page = await _go(clean_db, cheap, "sbp")
    assert page.status_code == 400 and bank.created == []


@pytest.mark.asyncio
async def test_a_wallet_the_bank_refuses_closes_the_payment_and_shows_a_page(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]

    async def refuse(method, payment_id, **_):
        raise tbank_acq.TBankError("off", code="3001")

    monkeypatch.setattr(tbank_acq, "wallet_link", refuse)
    page = await _go(clean_db, ticket, "sbp")
    assert page.status_code == 409 and "недоступен" in page.body.decode()
    assert bank.canceled == ["7710001"] and await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_the_bank_page_way_is_redirected_to_the_form_of_the_bank(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    response = await _go(clean_db, ticket, "page")
    assert (response.status_code, response.headers["location"]) == (302, "https://pay.test/abc")
    assert (await _go(clean_db, ticket, "page")).headers["location"] == "https://pay.test/abc"  # kept in the row
    assert len(bank.created) == 1


@pytest.mark.asyncio
async def test_a_paid_or_closed_payment_of_the_ticket_is_not_opened_again(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    await _go(clean_db, ticket, "sbp")
    await clean_db.execute("UPDATE yookassa_payments SET status = 'canceled'")
    page = await _go(clean_db, ticket, "sbp")
    assert page.status_code == 410 and len(bank.created) == 1
    await clean_db.execute("UPDATE yookassa_payments SET status = 'succeeded'")
    assert "уже прошла" in (await _go(clean_db, ticket, "sbp")).body.decode()


# ── the status of a ticket ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_ticket_without_a_payment_is_pending_and_belongs_to_its_buyer(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    other = await _tg_user(clean_db, 1999)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    assert await _status(clean_db, owner, ticket) == {"status": "pending", "generations": 10, "amount": 299}
    with pytest.raises(HTTPException) as stranger:
        await _status(clean_db, other, ticket)
    assert stranger.value.status_code == 404
    with pytest.raises(HTTPException) as forged:
        await _status(clean_db, owner, ticket[:-2] + "00")
    assert forged.value.status_code == 404


@pytest.mark.asyncio
async def test_the_ticket_status_follows_its_payments(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    ticket = (await _ticket(clean_db, owner))["ticket"]
    remote = {"status": "pending"}

    async def get_status(payment):
        return remote

    monkeypatch.setattr(miniapp_pay.payment_gateway, "get_payment_status", get_status)
    await _go(clean_db, ticket, "sbp")
    assert (await _status(clean_db, owner, ticket))["status"] == "pending"

    remote["status"] = "canceled"
    assert (await _status(clean_db, owner, ticket))["status"] == "canceled"

    # the buyer tried another way after the first closed: the ticket is open again
    remote["status"] = "pending"
    await clean_db.execute("UPDATE yookassa_payments SET status = 'canceled'")
    await _go(clean_db, ticket, "tpay")
    state = await _status(clean_db, owner, ticket)
    assert state["status"] == "pending"

    # any paid payment of the ticket is the answer
    await clean_db.execute("UPDATE yookassa_payments SET status = 'succeeded' WHERE yookassa_id = '7710001'")
    state = await _status(clean_db, owner, ticket)
    assert state["status"] == "succeeded" and state["amount"] == 299


@pytest.mark.asyncio
async def test_the_status_of_a_ticket_still_answers_after_it_expired(clean_db, bank, monkeypatch):
    owner = await _owner(clean_db, monkeypatch)
    token, _ = pay_ticket.issue(user_id=owner["user"]["id"], platform="telegram", external_id=1001, generations=10, amount_rubles=299,
                                context="menu", source=None, now=pay_ticket.time.time() - 1800)
    assert (await _status(clean_db, owner, token))["status"] == "pending"
    token, _ = pay_ticket.issue(user_id=owner["user"]["id"], platform="telegram", external_id=1001, generations=10, amount_rubles=299,
                                context="menu", source=None, now=pay_ticket.time.time() - 7200)
    with pytest.raises(HTTPException):
        await _status(clean_db, owner, token)
