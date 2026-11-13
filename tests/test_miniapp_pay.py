"""Mini App checkout: a payment confirmed from the Mini App's own method buttons or its widget."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioyookassa.exceptions import InvalidRequestError
from fastapi import HTTPException

from app.api.routes import miniapp_pay
from app.api.routes import payments as payments_route
from app.db import users_repo, yookassa_repo
from app.services import yookassa as yookassa_service

TIERS = [
    {"stars": 145, "rubles": 299, "generations": 10},
    {"stars": 299, "rubles": 499, "generations": 20},
]


@pytest.fixture
def checkout(monkeypatch):
    monkeypatch.setattr(miniapp_pay.settings, "YOOKASSA_SHOP_ID", 1)
    monkeypatch.setattr(miniapp_pay.settings, "YOOKASSA_SECRET_KEY", "secret")
    created = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        kind = kwargs.get("confirmation_type")
        return {
            "yookassa_id": f"yk_pay_{len(created)}",
            "confirmation_url": None if kind == "embedded" else f"https://pay.example/{kind}",
            "confirmation_token": "ct-token" if kind == "embedded" else None,
            "status": "pending",
        }

    monkeypatch.setattr(miniapp_pay.yookassa_service, "create_sbp_payment", fake_create)
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())
    return created


async def _seed_tiers(pool):
    await pool.execute(
        "INSERT INTO pricing (key, value) VALUES ('tiers', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
        json.dumps(TIERS),
    )


async def _tg_user(pool, telegram_id=1001):
    created = await users_repo.get_or_create_user_by_identity(pool, "telegram", str(telegram_id))
    await pool.execute("UPDATE users SET telegram_id = $2 WHERE id = $1", created["user"]["id"], telegram_id)
    data = await users_repo.get_user_by_telegram_id(pool, telegram_id)
    data["miniapp_identity"] = {"platform": "telegram", "external_id": telegram_id}
    return data


async def _max_user(pool, external_id="770001"):
    created = await users_repo.get_or_create_user_by_identity(pool, "max", external_id)
    data = await users_repo.get_user_by_id(pool, created["user"]["id"])
    data["miniapp_identity"] = {"platform": "max", "external_id": int(external_id)}
    return data


def _create(pool, user, **body):
    request = miniapp_pay.MiniAppYookassaCreateRequest(**{"generations": 10, "amount_rubles": 299, **body})
    return miniapp_pay.create_embedded_payment(request, miniapp_user=user, is_preview=False, pool=pool)


@pytest.mark.asyncio
async def test_telegram_checkout_stores_an_embedded_payment(clean_db, checkout):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)

    result = await _create(pool, user)

    assert result["payment_id"] == "yk_pay_1"
    assert result["confirmation_token"] == "ct-token"
    assert (result["amount"], result["generations"], result["bonus_generations"]) == (299, 10, 0)
    assert result["method"] == "widget" and result["confirmation_type"] == "embedded"
    assert result["confirmation_url"] is None
    assert checkout[0]["confirmation_type"] == "embedded" and checkout[0]["payment_method"] is None
    assert checkout[0]["locale"] == "ru_RU"
    assert checkout[0]["telegram_id"] == 1001 and checkout[0]["user_id"] == user["user"]["id"]
    row = await yookassa_repo.get_by_yookassa_id(pool, "yk_pay_1")
    assert (row["status"], row["platform"], row["confirmation_url"]) == ("pending", "telegram", None)
    assert (row["amount_rubles"], row["generations"], row["telegram_id"]) == (299, 10, 1001)


@pytest.mark.asyncio
async def test_max_checkout_uses_the_max_platform_and_no_telegram_id(clean_db, checkout):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _max_user(pool)

    result = await _create(pool, user, generations=20, amount_rubles=499)

    assert result["payment_id"] == "yk_pay_1"
    assert checkout[0]["telegram_id"] is None
    row = await yookassa_repo.get_by_yookassa_id(pool, "yk_pay_1")
    assert (row["platform"], row["telegram_id"], row["generations"]) == ("max", None, 20)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_user", [_tg_user, _max_user])
async def test_checkout_refuses_a_price_that_is_not_a_tier(clean_db, checkout, make_user):
    pool = clean_db
    await _seed_tiers(pool)
    user = await make_user(pool)

    with pytest.raises(HTTPException) as refused:
        await _create(pool, user, generations=10, amount_rubles=1)

    assert refused.value.status_code == 400
    assert checkout == []
    assert await pool.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_checkout_refuses_a_preview_session(clean_db, checkout):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)

    with pytest.raises(HTTPException) as refused:
        await miniapp_pay.create_embedded_payment(
            miniapp_pay.MiniAppYookassaCreateRequest(generations=10, amount_rubles=299),
            miniapp_user=user, is_preview=True, pool=pool,
        )

    assert refused.value.status_code == 403 and checkout == []


@pytest.mark.asyncio
async def test_a_payment_without_a_token_is_not_stored(clean_db, checkout, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)

    async def no_token(**_kwargs):
        return {"yookassa_id": "yk_no_token", "confirmation_url": None, "confirmation_token": None, "status": "pending"}

    monkeypatch.setattr(miniapp_pay.yookassa_service, "create_sbp_payment", no_token)

    with pytest.raises(HTTPException) as refused:
        await _create(pool, user)

    assert refused.value.status_code == 502
    assert await pool.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_a_runaway_loop_of_unpaid_payments_is_stopped(clean_db, checkout):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)

    for _ in range(miniapp_pay.MAX_RECENT_PENDING):
        await _create(pool, user)
    with pytest.raises(HTTPException) as refused:
        await _create(pool, user)

    assert refused.value.status_code == 429
    assert len(checkout) == miniapp_pay.MAX_RECENT_PENDING


def _remote(payment_id: str, user_id: int, *, status="succeeded", amount="299.00", telegram_id=None):
    metadata = {"user_id": str(user_id), "generations": "10"}
    if telegram_id is not None:
        metadata["telegram_id"] = str(telegram_id)
    return {
        "yookassa_id": payment_id, "status": status, "paid": status == "succeeded",
        "amount_value": amount, "currency": "RUB", "metadata": metadata, "payment_method": None,
    }


def _status(pool, user, payment_id):
    return miniapp_pay.get_payment_status(payment_id, SimpleNamespace(), miniapp_user=user, pool=pool)


@pytest.mark.asyncio
async def test_status_poll_settles_a_paid_payment_once(clean_db, checkout, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    await _create(pool, user)
    uid = user["user"]["id"]
    monkeypatch.setattr(
        miniapp_pay.yookassa_service, "get_payment_status",
        AsyncMock(return_value=_remote("yk_pay_1", uid, telegram_id=1001)),
    )
    # Notifications and analytics are other tests' business: settle for real, skip the side effects.
    finalize = AsyncMock(side_effect=lambda _request, _pool, paid, **_kwargs: paid)
    monkeypatch.setattr(payments_route, "_finalize_credited_payment", finalize)
    monkeypatch.setattr(miniapp_pay.yookassa_service, "matches_local_payment", yookassa_service.matches_local_payment)

    first = await _status(pool, user, "yk_pay_1")
    second = await _status(pool, user, "yk_pay_1")

    assert first["status"] == "succeeded" and first["credited_generations"] == 10
    assert first["balance"] == 11  # one free frame + the package
    assert second["status"] == "succeeded" and second["balance"] == 11
    assert finalize.await_count == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM payments WHERE user_id = $1", uid) == 1


@pytest.mark.asyncio
async def test_status_is_pending_while_the_provider_says_pending(clean_db, checkout, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    await _create(pool, user)
    monkeypatch.setattr(
        miniapp_pay.yookassa_service, "get_payment_status",
        AsyncMock(return_value=_remote("yk_pay_1", user["user"]["id"], status="pending")),
    )

    result = await _status(pool, user, "yk_pay_1")

    assert result == {"status": "pending", "generations": 10, "amount": 299}


@pytest.mark.asyncio
async def test_status_marks_a_canceled_payment(clean_db, checkout, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    await _create(pool, user)
    monkeypatch.setattr(
        miniapp_pay.yookassa_service, "get_payment_status",
        AsyncMock(return_value=_remote("yk_pay_1", user["user"]["id"], status="canceled")),
    )

    assert (await _status(pool, user, "yk_pay_1"))["status"] == "canceled"
    assert (await yookassa_repo.get_by_yookassa_id(pool, "yk_pay_1"))["status"] == "canceled"


@pytest.mark.asyncio
async def test_status_survives_an_unreachable_provider(clean_db, checkout, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    await _create(pool, user)
    monkeypatch.setattr(miniapp_pay.yookassa_service, "get_payment_status", AsyncMock(side_effect=RuntimeError("down")))

    assert (await _status(pool, user, "yk_pay_1"))["status"] == "pending"


@pytest.mark.asyncio
async def test_status_of_someone_elses_payment_is_not_found(clean_db, checkout):
    pool = clean_db
    await _seed_tiers(pool)
    owner, stranger = await _tg_user(pool, 1001), await _tg_user(pool, 1002)
    await _create(pool, owner)

    with pytest.raises(HTTPException) as refused:
        await _status(pool, stranger, "yk_pay_1")
    with pytest.raises(HTTPException) as unknown:
        await _status(pool, owner, "nope")

    assert refused.value.status_code == 404 and unknown.value.status_code == 404


@pytest.mark.asyncio
async def test_embedded_confirmation_asks_the_widget_and_redirect_is_untouched(monkeypatch):
    sent = []

    class FakePayments:
        async def create_payment(self, params):
            sent.append(params)
            confirmation = SimpleNamespace(url="https://pay/redirect", confirmation_token="ct-1")
            return SimpleNamespace(id=f"yk_{len(sent)}", status="pending", confirmation=confirmation)

    monkeypatch.setattr(yookassa_service, "get_client", lambda: SimpleNamespace(payments=FakePayments()))

    redirect = await yookassa_service.create_sbp_payment(299, "d", 10, user_id=1, return_url="https://max.ru/b")
    embedded = await yookassa_service.create_sbp_payment(299, "d", 10, user_id=1, telegram_id=7, embedded=True)
    by_type = await yookassa_service.create_sbp_payment(299, "d", 10, user_id=1, confirmation_type="embedded")

    assert sent[0].confirmation.type.value == "redirect" and sent[0].confirmation.return_url == "https://max.ru/b"
    assert set(redirect) == {"yookassa_id", "confirmation_url", "status"}
    assert sent[1].confirmation.type.value == "embedded" and sent[1].confirmation.return_url is None
    assert embedded["confirmation_token"] == "ct-1" and by_type["confirmation_token"] == "ct-1"
    assert sent[1].capture is True and sent[1].metadata["telegram_id"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, flow, payment_method, confirmation",
    [
        ("sberbank", "app", "sberbank", "mobile_application"),
        ("sberbank", "web", "sberbank", "redirect"),
        ("sbp", "app", "sbp", "redirect"),
        ("tinkoff_bank", "app", "tinkoff_bank", "redirect"),
        ("bank_card", "app", None, "embedded"),
        ("widget", "app", None, "embedded"),
    ],
)
async def test_each_button_asks_the_provider_for_its_method_and_confirmation(
    clean_db, checkout, method, flow, payment_method, confirmation,
):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)

    result = await _create(pool, user, method=method, flow=flow)

    sent = checkout[0]
    assert (sent["payment_method"], sent["confirmation_type"], sent["locale"]) == (payment_method, confirmation, "ru_RU")
    assert sent["extra_metadata"] == {"miniapp_method": method}
    assert result["method"] == method and result["confirmation_type"] == confirmation
    if confirmation == "embedded":
        assert result["confirmation_token"] == "ct-token" and result["confirmation_url"] is None
    else:
        assert result["confirmation_url"] == f"https://pay.example/{confirmation}" and result["confirmation_token"] is None
    row = await yookassa_repo.get_by_yookassa_id(pool, result["payment_id"])
    assert row["status"] == "pending" and row["confirmation_url"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("make_user, return_url", [
    (_tg_user, "https://t.me/your_bot?startapp=pay_return"),
    (_max_user, "https://max.ru/max_test_bot?startapp=pay_return"),
])
async def test_the_provider_returns_the_buyer_to_the_mini_app_of_the_same_platform(
    clean_db, checkout, monkeypatch, make_user, return_url,
):
    monkeypatch.setattr(miniapp_pay.settings, "BOT_USERNAME", "your_bot")
    monkeypatch.setattr(miniapp_pay.settings, "MAX_BOT_USERNAME", "max_test_bot")
    pool = clean_db
    await _seed_tiers(pool)
    user = await make_user(pool)

    await _create(pool, user, method="sbp")

    assert checkout[0]["return_url"] == return_url


@pytest.mark.asyncio
async def test_a_confirmation_the_shop_may_not_use_falls_back_to_the_providers_page(clean_db, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    monkeypatch.setattr(miniapp_pay.settings, "YOOKASSA_SHOP_ID", 1)
    monkeypatch.setattr(miniapp_pay.settings, "YOOKASSA_SECRET_KEY", "secret")
    monkeypatch.setattr(miniapp_pay.analytics, "track", AsyncMock())
    tried = []

    async def deeplink_not_allowed(**kwargs):
        tried.append(kwargs["confirmation_type"])
        if kwargs["confirmation_type"] == "mobile_application":
            raise InvalidRequestError("Unsupported confirmation type")
        return {"yookassa_id": "yk_web", "confirmation_url": "https://pay.example/web", "confirmation_token": None, "status": "pending"}

    monkeypatch.setattr(miniapp_pay.yookassa_service, "create_sbp_payment", deeplink_not_allowed)

    result = await _create(pool, user, method="sberbank")

    assert tried == ["mobile_application", "redirect"]
    assert (result["confirmation_type"], result["confirmation_url"]) == ("redirect", "https://pay.example/web")


@pytest.mark.asyncio
async def test_a_method_the_shop_cannot_use_at_all_is_a_provider_error(clean_db, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    monkeypatch.setattr(miniapp_pay.settings, "YOOKASSA_SHOP_ID", 1)
    monkeypatch.setattr(miniapp_pay.settings, "YOOKASSA_SECRET_KEY", "secret")
    monkeypatch.setattr(
        miniapp_pay.yookassa_service, "create_sbp_payment",
        AsyncMock(side_effect=InvalidRequestError("payment_method_not_available")),
    )

    with pytest.raises(HTTPException) as refused:
        await _create(pool, user, method="tinkoff_bank")

    assert refused.value.status_code == 502
    assert await pool.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_a_link_method_without_a_link_is_not_stored(clean_db, checkout, monkeypatch):
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    monkeypatch.setattr(
        miniapp_pay.yookassa_service, "create_sbp_payment",
        AsyncMock(return_value={"yookassa_id": "yk_no_link", "confirmation_url": None, "status": "pending"}),
    )

    with pytest.raises(HTTPException) as refused:
        await _create(pool, user, method="sbp")

    assert refused.value.status_code == 502
    assert await pool.fetchval("SELECT COUNT(*) FROM yookassa_payments") == 0


@pytest.mark.asyncio
async def test_the_method_button_reaches_the_provider_payment(monkeypatch):
    sent = []

    class FakePayments:
        async def create_payment(self, params):
            sent.append(params)
            return SimpleNamespace(
                id="yk_1", status="pending",
                confirmation=SimpleNamespace(url="sberpay://invoicing/v2?x=1", confirmation_token=None, confirmation_data=None),
            )

    monkeypatch.setattr(yookassa_service, "get_client", lambda: SimpleNamespace(payments=FakePayments()))

    result = await yookassa_service.create_sbp_payment(
        299, "d", 10, user_id=1, return_url="https://t.me/b?startapp=pay_return",
        payment_method="sberbank", confirmation_type="mobile_application", locale="ru_RU",
        extra_metadata={"miniapp_method": "sberbank"},
    )

    params = sent[0]
    assert params.payment_method_data.type == "sberbank"
    assert (params.confirmation.type.value, params.confirmation.locale) == ("mobile_application", "ru_RU")
    assert params.confirmation.return_url == "https://t.me/b?startapp=pay_return"
    assert params.metadata == {"user_id": "1", "generations": "10", "miniapp_method": "sberbank"}
    assert result["confirmation_url"] == "sberpay://invoicing/v2?x=1"


@pytest.mark.asyncio
async def test_an_sbp_qr_confirmation_link_comes_from_confirmation_data(monkeypatch):
    class FakePayments:
        async def create_payment(self, params):
            confirmation = SimpleNamespace(url=None, confirmation_token=None, confirmation_data="https://qr.nspk.ru/AB1")
            return SimpleNamespace(id="yk_qr", status="pending", confirmation=confirmation)

    monkeypatch.setattr(yookassa_service, "get_client", lambda: SimpleNamespace(payments=FakePayments()))

    result = await yookassa_service.create_sbp_payment(
        299, "d", 10, user_id=1, payment_method="sbp", confirmation_type="qr",
    )

    assert result["confirmation_url"] == "https://qr.nspk.ru/AB1"


# --- Package picker inside the Mini App (flag MINIAPP_PAYWALL_INAPP) ---------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from app.services import discount_ladder  # noqa: E402


def _packages(pool, user):
    return miniapp_pay.get_packages(SimpleNamespace(headers={}), miniapp_user=user, pool=pool)


@pytest.mark.asyncio
async def test_packages_are_off_with_the_flag_off(clean_db, monkeypatch):
    monkeypatch.setattr(miniapp_pay.settings, "MINIAPP_PAYWALL_INAPP", False)
    await _seed_tiers(clean_db)

    assert await _packages(clean_db, await _tg_user(clean_db)) == {"enabled": False, "tiers": [], "offer": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["telegram", "max"])
async def test_packages_are_the_bots_menu_with_promo_and_bonus(clean_db, monkeypatch, platform):
    """Same call both bots make for «купить энергию»: the discount and the bonus come with it, Stars do not."""
    monkeypatch.setattr(miniapp_pay.settings, "MINIAPP_PAYWALL_INAPP", True)
    pool = clean_db
    await _seed_tiers(pool)
    for key, value in {
        "promo_discount_active": "true", "promo_discount_by_generations": json.dumps({"20": 20}),
        "promo_bonus_active": "true", "promo_bonus_by_generations": json.dumps({"10": 3}),
    }.items():
        await pool.execute(
            "INSERT INTO pricing (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2", key, value,
        )
    user = await (_tg_user(pool) if platform == "telegram" else _max_user(pool))

    result = await _packages(pool, user)

    assert result["enabled"] is True and result["offer"] is None
    assert all("stars" not in tier for tier in result["tiers"])
    bot_menu = await payments_route._price_tiers(1001, "menu", None, pool)
    assert [(t["generations"], t["rubles"], t["original_rubles"], t["bonus_generations"]) for t in result["tiers"]] == [
        (t.generations, t.rubles, t.original_rubles, t.bonus_generations) for t in bot_menu.tiers
    ]
    assert result["tiers"][0]["bonus_generations"] == 3
    assert result["tiers"][1]["rubles"] < 499 and result["tiers"][1]["original_rubles"] == 499


@pytest.mark.asyncio
async def test_a_live_winback_offer_is_listed_and_buyable_with_its_context(clean_db, checkout, monkeypatch):
    monkeypatch.setattr(miniapp_pay.settings, "MINIAPP_PAYWALL_INAPP", True)
    monkeypatch.setattr(miniapp_pay.settings, "WINBACK_OFFER_ENABLED", True)
    pool = clean_db
    await _seed_tiers(pool)
    user = await _tg_user(pool)
    assert (await _packages(pool, user))["offer"] is None
    await pool.execute(
        "INSERT INTO discount_offers (user_id, slug, platform, expires_at) VALUES ($1, $2, 'telegram', $3)",
        user["user"]["id"], discount_ladder.WINBACK_SLUG, datetime.now(timezone.utc) + timedelta(hours=5),
    )

    offer = (await _packages(pool, user))["offer"]

    assert offer["context"] == f"discount:{discount_ladder.WINBACK_SLUG}"
    assert [(t["generations"], t["rubles"], t["original_rubles"]) for t in offer["tiers"]] == [(20, 299, 499)]
    result = await _create(pool, user, generations=20, amount_rubles=299, context=offer["context"], method="redirect")
    assert result["confirmation_type"] == "redirect" and result["amount"] == 299
    row = await yookassa_repo.get_by_yookassa_id(pool, result["payment_id"])
    assert row["offer_slug"] == discount_ladder.WINBACK_SLUG and row["platform"] == "telegram"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, expected", [("telegram", "telegram"), ("max", "max")])
async def test_redirect_checkout_is_the_bots_payment_page_with_the_bots_platform(
    clean_db, checkout, platform, expected,
):
    pool = clean_db
    await _seed_tiers(pool)
    user = await (_tg_user(pool) if platform == "telegram" else _max_user(pool))

    result = await _create(pool, user, method="redirect", source="header")

    assert result["confirmation_type"] == "redirect" and result["confirmation_token"] is None
    assert result["confirmation_url"] == "https://pay.example/redirect"
    assert checkout[0]["payment_method"] is None and checkout[0]["confirmation_type"] == "redirect"
    row = await yookassa_repo.get_by_yookassa_id(pool, result["payment_id"])
    assert row["platform"] == expected and row["confirmation_url"] == "https://pay.example/redirect"
    names = [call.args[0] for call in miniapp_pay.analytics.track.await_args_list]
    assert names == ["miniapp_pack_selected", "yookassa_payment_created"]
    selected = miniapp_pay.analytics.track.await_args_list[0].kwargs["properties"]
    assert (selected["generations"], selected["amount"], selected["source"]) == (10, 299, "header")


@pytest.mark.asyncio
async def test_redirect_checkout_still_refuses_a_forged_price(clean_db, checkout):
    await _seed_tiers(clean_db)
    user = await _max_user(clean_db)

    with pytest.raises(HTTPException) as refused:
        await _create(clean_db, user, method="redirect", amount_rubles=1)

    assert refused.value.status_code == 400 and checkout == []


@pytest.mark.asyncio
async def test_paywall_opened_is_tracked_except_in_preview(clean_db, monkeypatch):
    track = AsyncMock()
    monkeypatch.setattr(miniapp_pay.analytics, "track", track)
    user = await _max_user(clean_db)

    await miniapp_pay.post_paywall_opened(miniapp_pay.PaywallOpened(source="edit"), miniapp_user=user, is_preview=False)
    await miniapp_pay.post_paywall_opened(miniapp_pay.PaywallOpened(source="edit"), miniapp_user=user, is_preview=True)

    assert track.await_count == 1 and track.await_args.args[0] == "miniapp_paywall_opened"
    assert track.await_args.kwargs["properties"] == {"platform": "max", "source": "edit"}
