"""The Mini App's own ways to pay for a T-Bank payment: the link of SBP / SberPay / T-Pay, the card in a frame,
the probe that decides which buttons exist. Answers of the API are those seen on the terminals on 10.11."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.routes import miniapp_pay
from app.core.config import settings
from app.services import tbank_acq, tbank_methods
from tests.test_miniapp_pay import _seed_tiers, _tg_user  # noqa: F401
from tests.test_tbank_acquiring import PASSWORD, TERMINAL, tbank_settings  # noqa: F401
from tests.test_tbank_test_mode import _create, _fake_create, test_mode  # noqa: F401

REFUSED_11 = {"Success": False, "ErrorCode": "11", "Message": "Невозможно выполнить платеж"}
REFUSED_3001 = {"Success": False, "ErrorCode": "3001", "Message": "Оплата выбранным способом недоступна"}


@pytest.fixture(autouse=True)
def fresh_probe():
    tbank_methods.reset()
    yield
    tbank_methods.reset()


def transport(handler_map, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path.split("/v2", 1)[1])
        if calls is not None:
            calls.append((key, json.loads(request.content) if request.content else None))
        answer = handler_map[key]
        return httpx.Response(200, json=answer)
    return httpx.MockTransport(handler)


# ── the API client ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sbp_link_is_a_signed_getqr_for_the_payload():
    calls = []
    t = transport({("POST", "/GetQr"): {"Success": True, "ErrorCode": "0", "PaymentId": 9611790839, "Data": "https://qr.nspk.ru/BD1"}}, calls)
    assert await tbank_acq.sbp_link("9611790839", transport=t) == "https://qr.nspk.ru/BD1"
    body = calls[0][1]
    assert (body["PaymentId"], body["DataType"], body["TerminalKey"]) == ("9611790839", "PAYLOAD", TERMINAL)
    assert tbank_acq.verify_notification(body, PASSWORD)  # signed like every POST


@pytest.mark.asyncio
async def test_a_refused_link_is_a_bank_error_with_its_code_and_a_non_https_link_is_not_taken():
    t = transport({("POST", "/GetQr"): REFUSED_3001})
    with pytest.raises(tbank_acq.TBankError) as refused:
        await tbank_acq.sbp_link("1", transport=t)
    assert refused.value.code == "3001"
    odd = transport({("POST", "/GetQr"): {"Success": True, "Data": "javascript:alert(1)"}})
    with pytest.raises(tbank_acq.TBankError):
        await tbank_acq.sbp_link("1", transport=odd)


@pytest.mark.asyncio
async def test_tpay_asks_the_terminal_then_the_link_and_the_get_calls_are_not_signed():
    calls = []
    t = transport({
        ("GET", f"/TinkoffPay/terminals/{TERMINAL}/status"): {"Success": True, "ErrorCode": "0", "Params": {"Allowed": True, "Version": "2.0"}},
        ("GET", "/TinkoffPay/transactions/77/versions/2.0/link"): {"Success": True, "ErrorCode": "0", "Params": {"RedirectUrl": "https://www.tinkoff.ru/tpay/xyz"}},
    }, calls)
    assert await tbank_acq.wallet_link("tinkoff_bank", "77", transport=t) == "https://www.tinkoff.ru/tpay/xyz"
    assert [key for key, _ in calls] == [("GET", f"/TinkoffPay/terminals/{TERMINAL}/status"), ("GET", "/TinkoffPay/transactions/77/versions/2.0/link")]
    assert all(body is None for _, body in calls)


@pytest.mark.asyncio
async def test_tpay_that_the_terminal_may_not_use_is_not_asked_for_a_link():
    calls = []
    t = transport({("GET", f"/TinkoffPay/terminals/{TERMINAL}/status"): {"Success": True, "ErrorCode": "0", "Params": {"Allowed": False}}}, calls)
    with pytest.raises(tbank_acq.TBankError) as refused:
        await tbank_acq.wallet_link("tinkoff_bank", "77", transport=t)
    assert refused.value.code == "tpay_not_allowed" and len(calls) == 1


@pytest.mark.asyncio
async def test_sberpay_link_is_the_redirect_url_of_the_transaction():
    ok = {"Success": True, "ErrorCode": "0", "Params": {"RedirectUrl": "sberpay://invoicing?bankInvoiceId=abc"}}
    assert await tbank_acq.wallet_link("sberbank", "88", transport=transport({("GET", "/SberPay/transactions/88/link"): ok})) == "sberpay://invoicing?bankInvoiceId=abc"
    with pytest.raises(tbank_acq.TBankError) as refused:
        await tbank_acq.wallet_link("sberbank", "88", transport=transport({("GET", "/SberPay/transactions/88/link"): REFUSED_11}))
    assert refused.value.code == "11"


@pytest.mark.asyncio
async def test_init_carries_the_wallet_flags_and_a_probe_has_no_notification():
    calls = []
    init_ok = {"Success": True, "ErrorCode": "0", "Status": "NEW", "PaymentId": "5", "PaymentURL": "https://pay.test/x"}
    await tbank_acq.create_payment(
        10, "p", 0, user_id=0, order_id="gl-probe-1", notify=False, due_in_minutes=5,
        device_data=tbank_acq.wallet_device_data("sberbank", mobile=True), transport=transport({("POST", "/Init"): init_ok}, calls),
    )
    body = calls[0][1]
    assert body["DATA"]["SberPayWeb"] is True and body["DATA"]["Device"] == "Mobile"
    assert "NotificationURL" not in body and body["RedirectDueDate"].endswith("+00:00")
    assert tbank_acq.wallet_device_data("tinkoff_bank", mobile=False) == {"Device": "Desktop", "TinkoffPayWeb": True}
    assert tbank_acq.wallet_device_data("sbp", mobile=True) == {"Device": "Mobile"}


# ── the probe ──────────────────────────────────────────────────────

def fake_bank(monkeypatch, open_methods, *, init_fails=False):
    log = []

    async def create_payment(*args, **kwargs):
        log.append("init")
        if init_fails:
            raise tbank_acq.TBankError("down")
        return {"yookassa_id": "900", "confirmation_url": "https://pay.test/x", "status": "pending", "order_id": kwargs["order_id"], "provider": "tbank"}

    async def wallet_link(method, payment_id, **_):
        log.append(method)
        if method not in open_methods:
            raise tbank_acq.TBankError("off", code="11")
        return f"https://bank.test/{method}"

    async def cancel_payment(payment_id, *_, **__):
        log.append("cancel")
        return {}

    monkeypatch.setattr(tbank_acq, "create_payment", create_payment)
    monkeypatch.setattr(tbank_acq, "wallet_link", wallet_link)
    monkeypatch.setattr(tbank_acq, "cancel_payment", cancel_payment)
    return log


@pytest.mark.asyncio
async def test_the_probe_shows_only_the_ways_the_bank_gave_a_link_for_in_the_order_of_the_buttons(monkeypatch):
    log = fake_bank(monkeypatch, {"sbp", "sberbank"})
    assert await tbank_methods.available_wallets() == ["sberbank", "sbp"]
    assert log == ["init", "tinkoff_bank", "sberbank", "sbp", "cancel"]  # the QR is asked last
    # fresh: no second probe, and several callers at once share one
    assert await asyncio.gather(*[tbank_methods.available_wallets() for _ in range(3)]) == [["sberbank", "sbp"]] * 3
    assert log.count("init") == 1


@pytest.mark.asyncio
async def test_a_terminal_with_nothing_enabled_has_no_wallets_and_a_dead_bank_is_not_hammered(monkeypatch):
    log = fake_bank(monkeypatch, set())
    assert await tbank_methods.available_wallets() == []
    tbank_methods.reset()
    log = fake_bank(monkeypatch, {"sbp"}, init_fails=True)
    assert await tbank_methods.available_wallets() == []
    assert await tbank_methods.available_wallets() == []
    assert log == ["init"]  # one try per minute


@pytest.mark.asyncio
async def test_a_refusal_at_checkout_closes_the_button_until_the_next_probe_window(monkeypatch):
    fake_bank(monkeypatch, {"sbp", "sberbank", "tinkoff_bank"})
    assert await tbank_methods.available_wallets() == ["sberbank", "sbp", "tinkoff_bank"]
    tbank_methods.mark_unavailable("sberbank")
    assert await tbank_methods.available_wallets() == ["sbp", "tinkoff_bank"]


@pytest.mark.asyncio
async def test_the_preview_can_assume_the_wallets_without_asking_the_bank(monkeypatch):
    log = fake_bank(monkeypatch, set())
    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", "tinkoff_bank, sbp,nonsense")
    assert await tbank_methods.available_wallets() == ["sbp", "tinkoff_bank"]  # in the order of the buttons
    assert log == []


# ── checkout ───────────────────────────────────────────────────────

@pytest.fixture
def owner_sheet(clean_db, test_mode, monkeypatch):
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())
    created = []
    monkeypatch.setattr(tbank_acq, "create_payment", _fake_create(created))
    canceled = []

    async def cancel(payment_id, *_, **__):
        canceled.append(payment_id)
        return {}

    monkeypatch.setattr(tbank_acq, "cancel_payment", cancel)
    return created, canceled


async def _owner(pool, monkeypatch, telegram_id):
    await _seed_tiers(pool)
    owner = await _tg_user(pool, telegram_id)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", str(owner["user"]["id"]))
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("method, flow, flags", [
    ("sbp", "app", {"Device": "Mobile"}),
    ("sberbank", "app", {"Device": "Mobile", "SberPayWeb": True}),
    ("tinkoff_bank", "web", {"Device": "Desktop", "TinkoffPayWeb": True}),
])
async def test_a_wallet_button_inits_with_its_flags_and_hands_over_the_link(clean_db, owner_sheet, monkeypatch, method, flow, flags):
    created, canceled = owner_sheet
    owner = await _owner(clean_db, monkeypatch, 1101)
    monkeypatch.setattr(tbank_acq, "wallet_link", AsyncMock(return_value="https://bank.test/pay"))

    result = await _create(clean_db, owner, method=method, flow=flow, embed=True, return_to="https://app.your-domain.example/max/")

    assert created[0]["device_data"] == flags
    assert (result["confirmation_type"], result["confirmation_url"], result["embed"], result["top_level"]) == (
        "app_link", "https://bank.test/pay", False, False)
    assert "&to=" not in created[0]["return_url"]  # a wallet never takes the buyer to the top-level page
    assert tbank_acq.wallet_link.await_args.args == (method, "7710001")
    assert await clean_db.fetchval("SELECT status FROM yookassa_payments WHERE yookassa_id = '7710001'") == "pending"
    assert not canceled


@pytest.mark.asyncio
async def test_a_wallet_the_bank_refuses_is_canceled_not_stored_and_its_button_closes(clean_db, owner_sheet, monkeypatch):
    created, canceled = owner_sheet
    owner = await _owner(clean_db, monkeypatch, 1102)
    monkeypatch.setattr(tbank_acq, "wallet_link", AsyncMock(side_effect=tbank_acq.TBankError("off", code="3001")))
    monkeypatch.setattr(settings, "TBANK_WALLETS_ASSUME", "")

    with pytest.raises(HTTPException) as refused:
        await _create(clean_db, owner, method="sbp", flow="app")
    assert (refused.value.status_code, refused.value.detail) == (409, "method_unavailable")
    assert canceled == ["7710001"]
    assert await clean_db.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0
    assert tbank_methods._state["sbp"][1] is False


@pytest.mark.asyncio
async def test_a_hiccup_of_the_bank_is_a_provider_error_and_leaves_the_button_alone(clean_db, owner_sheet, monkeypatch):
    created, canceled = owner_sheet
    owner = await _owner(clean_db, monkeypatch, 1103)
    monkeypatch.setattr(tbank_acq, "wallet_link", AsyncMock(side_effect=tbank_acq.TBankError("timeout", unknown=True)))
    with pytest.raises(HTTPException) as failed:
        await _create(clean_db, owner, method="sberbank", flow="app")
    assert failed.value.status_code == 502 and canceled == ["7710001"]
    assert "sberbank" not in tbank_methods._state


@pytest.mark.asyncio
async def test_the_card_and_the_bank_page_are_the_form_in_the_frame_and_it_is_not_top_level_by_default(clean_db, owner_sheet, monkeypatch):
    created, _ = owner_sheet
    owner = await _owner(clean_db, monkeypatch, 1104)
    for method in ("bank_card", "redirect"):
        result = await _create(clean_db, owner, method=method, flow="web", embed=True, return_to="https://app.your-domain.example/max/")
        assert (result["confirmation_type"], result["embed"], result["top_level"]) == ("redirect", True, False)
        assert "device_data" not in created[-1] or not created[-1]["device_data"]
    assert settings.TBANK_TOP_LEVEL is False and settings.TBANK_BUTTONS is True


# ── the sheet ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_sheet_lists_the_ways_to_pay_and_a_bank_page_when_no_wallet_is_open(clean_db, test_mode, monkeypatch):
    await _seed_tiers(clean_db)
    owner, stranger = await _tg_user(clean_db, 1105), await _tg_user(clean_db, 1106)
    monkeypatch.setattr(settings, "TBANK_TEST_USER_IDS", str(owner["user"]["id"]))
    packages = lambda user: miniapp_pay.get_packages(SimpleNamespace(headers={}), miniapp_user=user, pool=clean_db)  # noqa: E731

    fake_bank(monkeypatch, {"sbp"})
    assert (await packages(owner))["pay_methods"] == {"wallets": ["sbp"], "card": True, "bank_page": False}
    tbank_methods.reset()
    fake_bank(monkeypatch, set())
    assert (await packages(owner))["pay_methods"] == {"wallets": [], "card": True, "bank_page": True}
    # YooKassa keeps its single button; the switch brings the old sheet back
    assert (await packages(stranger))["pay_methods"] is None
    monkeypatch.setattr(settings, "TBANK_BUTTONS", False)
    assert (await packages(owner))["pay_methods"] is None


# ── SberPay: the bank's link is a custom scheme, the client gets an https page of ours ──

DEEPLINK = "sberpay://invoicing/v2?bankInvoiceId=01a1c4b462b359ab199781ed7f7db029&operationType=Web2App"


def test_pay_open_url_wraps_a_custom_scheme_and_leaves_https_alone():
    from urllib.parse import parse_qs, urlsplit
    from app.api.routes.miniapp_pay import pay_open_url

    assert pay_open_url("https://qr.nspk.ru/AS1", True) == "https://qr.nspk.ru/AS1"
    wrapped = urlsplit(pay_open_url(DEEPLINK, True))
    assert wrapped.scheme == "https" and wrapped.path.endswith("/pay-open")
    assert parse_qs(wrapped.query) == {"p": ["max"], "u": [DEEPLINK]}


@pytest.mark.asyncio
async def test_sberpay_deeplink_reaches_the_client_as_an_https_page(clean_db, owner_sheet, monkeypatch):
    owner = await _owner(clean_db, monkeypatch, 1110)
    monkeypatch.setattr(tbank_acq, "wallet_link", AsyncMock(return_value=DEEPLINK))
    result = await _create(clean_db, owner, method="sberbank", flow="app", embed=True)
    assert result["confirmation_type"] == "app_link"
    assert result["confirmation_url"].startswith("https://") and "/pay-open?" in result["confirmation_url"]
    assert "sberpay%3A%2F%2Finvoicing" in result["confirmation_url"]


@pytest.mark.asyncio
async def test_pay_open_page_has_the_button_and_refuses_foreign_links():
    from app.api.routes.miniapp_pay import pay_open

    page = await pay_open(u=DEEPLINK, p="max")
    body = page.body.decode()
    assert 'id="go" href="sberpay://invoicing/v2?bankInvoiceId=01a1c4b462b359ab199781ed7f7db029&amp;operationType=Web2App"' in body
    assert "max.ru/" in body and "Открыть Сбер" in body
    for bad in ("javascript:alert(1)", "sberpay://evil/x", "https://x.test/", "sberpay://invoicing/v2?a=1</script>", ""):
        with pytest.raises(HTTPException) as refused:
            await pay_open(u=bad, p="tg")
        assert refused.value.status_code == 400
