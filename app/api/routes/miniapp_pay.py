"""Mini App checkout: a YooKassa payment confirmed from the Mini App's own screen.

The bots create their payments with a redirect and send the buyer to an external
browser. Here the buyer picks the way to pay on the Mini App's screen (SberPay,
SBP, T-Pay, bank card) and the payment is created for that method: a bank-app
deeplink or a provider page for the three wallets, YooKassa's checkout widget
(``confirmation.type=embedded``, token in the response) for the card and for the
"all methods" entry. Tier checks, the menu bonus, the stored row, the webhook and
the settlement are the ones the bots already use: only the confirmation differs.
"""

import base64
import json
import logging
import re
from html import escape as html_escape
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlsplit

import asyncpg
from aioyookassa.exceptions import InvalidRequestError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api.deps import get_db
from app.api.routes import max as max_routes
from app.api.routes import payments as payments_routes
from app.api.routes.miniapp import get_miniapp_user, is_miniapp_preview
from app.core import edge
from app.core.config import settings
from app.db import discount_ladder_repo, pricing_repo, users_repo, yookassa_repo
from app.services import discount_ladder
from app.services import yookassa as yookassa_service
from app.services import bot_pay, pay_ticket, payment_gateway, tbank_acq, tbank_methods
from app.services.analytics import analytics, analytics_user_id

router = APIRouter()
logger = logging.getLogger(__name__)

# A buyer who keeps reopening the screen leaves unpaid payments behind; stop a runaway loop.
MAX_RECENT_PENDING = 5
RECENT_PENDING_WINDOW = timedelta(minutes=30)


# What each button asks YooKassa for: (payment_method_data type, confirmation types to try
# in order). A confirmation type the shop is not allowed to use is rejected with
# ``invalid_request`` and the next one is tried, so a missing permission degrades to the
# provider's page instead of an error. ``flow="web"`` drops the deeplink variants (desktop
# clients, or the buyer's second try after the bank app did not open).
METHOD_PLANS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "sberbank": ("sberbank", ("mobile_application", "redirect")),
    "sbp": ("sbp", ("redirect",)),
    "tinkoff_bank": ("tinkoff_bank", ("redirect",)),
    # The card has no embedded variant of its own (the API refuses bank_card + embedded) and
    # the shop may not restrict the widget to cards: both open the widget with every method.
    "bank_card": (None, ("embedded",)),
    "widget": (None, ("embedded",)),
    # The bots' own checkout: the provider's ordinary page with its method chooser (no method preset).
    "redirect": (None, ("redirect",)),
}
DEEPLINK_CONFIRMATIONS = {"mobile_application", "qr"}
# The widget and the provider's pages open in the buyer's language, not the phone's.
PAYMENT_LOCALE = "ru_RU"


class MiniAppYookassaCreateRequest(BaseModel):
    generations: int
    amount_rubles: int
    context: str = "menu"
    method: Literal["sberbank", "sbp", "tinkoff_bank", "bank_card", "widget", "redirect"] = "widget"
    flow: Literal["app", "web"] = "app"
    # The client can show the provider's page inside the app (iframe); T-Bank only, others ignore it.
    embed: bool = False
    # Address of the Mini App page the buyer leaves from (origin + path, no query). T-Bank's SuccessURL/FailURL
    # send the buyer back to it, so the form can open in the same WebView instead of an iframe. T-Bank only.
    return_to: str | None = Field(default=None, max_length=300)
    # Where the package picker was opened from (analytics only).
    source: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_]+$")


def _return_url(is_max: bool) -> str:
    """Where the provider's page sends the buyer back to: this Mini App, on its catalog."""
    if is_max:
        return f"https://max.ru/{settings.MAX_BOT_USERNAME}?startapp=pay_return"
    return f"https://t.me/{settings.BOT_USERNAME}?startapp=pay_return"


_RETURN_PATH = re.compile(r"^/[A-Za-z0-9_\-./]{0,200}$")


def _return_origins() -> set[str]:
    """Origins the Mini App is served from (direct and through the API Gateway edge): only these may be returned to."""
    origins = set()
    for url in (settings.TBANK_RETURN_URL, "https://app.your-domain.example", edge.EDGE_MEDIA_URL, *settings.MINIAPP_RETURN_ORIGINS.split(",")):
        parts = urlsplit((url or "").strip())
        if parts.scheme == "https" and parts.netloc:
            origins.add(f"https://{parts.netloc}")
    return origins


def safe_return_to(value: str | None) -> str | None:
    """``https://<our origin>/<path>`` or None. The value comes from a URL, so anything else (other host, query,
    fragment, odd characters) is dropped: the return page must never be an open redirect."""
    if not value:
        return None
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or parts.query or parts.fragment or not _RETURN_PATH.match(parts.path or "/"):
        return None
    origin = f"https://{parts.netloc}"
    if origin not in _return_origins():
        return None
    return origin + (parts.path or "/")


def _tbank_return_urls(is_max: bool, return_to: str | None = None) -> tuple[str, str]:
    """SuccessURL / FailURL of T-Bank: our page that tells the Mini App the form is over (see ``pay_return``)."""
    base = f"{settings.TBANK_RETURN_URL}?p={'max' if is_max else 'tg'}"
    if return_to:
        base += f"&to={quote(return_to, safe='')}"
    return f"{base}&ok=1", f"{base}&ok=0"


# The bank's SberPay link is a custom scheme (``sberpay://invoicing/v2?bankInvoiceId=…``, whatever Device/DeviceOs/
# DeviceWebView the Init says, checked 10.11): the WebView of MAX/Telegram does not open it, and ``openLink`` takes https only.
# So the client gets an https page of ours (``pay-open``) and opens it in the external browser, where the scheme works.
_APP_SCHEME_LINK = re.compile(r"^sberpay://invoicing/v2\?[A-Za-z0-9=&_\-]{1,200}$")


def pay_open_url(link: str, is_max: bool) -> str:
    """https page that opens the bank app for a custom-scheme link (``pay_open``); https links are used as they are."""
    if link.lower().startswith("https://"):
        return link
    base = settings.TBANK_RETURN_URL.rsplit("/", 1)[0]
    return f"{base}/pay-open?p={'max' if is_max else 'tg'}&u={quote(link, safe='')}"


class WalletUnavailable(Exception):
    """T-Bank gave no link for the way to pay (not enabled for the terminal): the button must disappear."""


async def _create_for_method(
    *, method: str, flow: str, is_max: bool, amount_rubles: int, generations: int, user_id: int,
    telegram_id: int | None, provider: str = payment_gateway.YOOKASSA, return_to: str | None = None,
) -> tuple[dict, str]:
    """Create the provider payment for a button; returns (result, confirmation type used).

    T-Bank: the wallet buttons (SBP, SberPay, T-Pay) ``Init`` the payment and take the link of that wallet
    (confirmation ``app_link``, the client opens it outside the app); the card is T-Bank's page in an iframe of the sheet
    and every other button gets that page too (``redirect``). If T-Bank fails at ``Init`` the error propagates (the caller
    answers 502; there is no switch to YooKassa); a wallet the bank refuses a link for is ``WalletUnavailable`` (the payment is canceled).
    """
    if provider == payment_gateway.TBANK:
        success_url, fail_url = _tbank_return_urls(is_max, return_to)
        wallet = settings.TBANK_BUTTONS and method in tbank_acq.WALLET_METHODS
        result = await payment_gateway.create_payment(
            provider,
            amount_rubles=amount_rubles,
            description=f"{settings.BRAND_NAME} — пакет {generations} энергии",
            generations=generations,
            user_id=user_id,
            telegram_id=telegram_id,
            return_url=success_url,
            fail_url=fail_url,
            extra_metadata={"miniapp_method": method},
            device_data=tbank_acq.wallet_device_data(method, mobile=flow == "app") if wallet else None,
        )
        if not wallet:
            return result, "redirect"
        try:
            link = await tbank_acq.wallet_link(method, result["yookassa_id"])
        except tbank_acq.TBankError as exc:
            logger.warning("Mini App T-Bank %s link refused: user=%s code=%s", method, user_id, exc.code)
            try:
                await tbank_acq.cancel_payment(result["yookassa_id"])
            except tbank_acq.TBankError:
                logger.warning("Mini App T-Bank: could not cancel %s", result["yookassa_id"], exc_info=True)
            if not exc.code:
                raise  # a hiccup of the bank or the network, not a closed way: the button stays
            tbank_methods.mark_unavailable(method)
            raise WalletUnavailable(method) from exc
        return {**result, "confirmation_url": pay_open_url(link, is_max)}, "app_link"
    payment_method, confirmations = METHOD_PLANS[method]
    if flow == "web":
        confirmations = tuple(kind for kind in confirmations if kind not in DEEPLINK_CONFIRMATIONS)
    for index, kind in enumerate(confirmations):
        try:
            result = await payment_gateway.create_payment(
                payment_gateway.YOOKASSA,
                amount_rubles=amount_rubles,
                description=f"{settings.BRAND_NAME} — пакет {generations} энергии",
                generations=generations,
                user_id=user_id,
                telegram_id=telegram_id,
                return_url=_return_url(is_max),
                payment_method=payment_method,
                confirmation_type=kind,
                locale=PAYMENT_LOCALE,
                extra_metadata={"miniapp_method": method},
            )
        except InvalidRequestError:
            # The shop may lack this method/confirmation: try the next, plainer one.
            logger.warning(
                "Mini App YooKassa create: %s with %s rejected", method, kind, exc_info=True,
            )
            if index == len(confirmations) - 1:
                raise
            continue
        return result, kind
    raise RuntimeError("unreachable")  # METHOD_PLANS always has at least one confirmation


async def _checked_pack(
    pool: asyncpg.Pool, user: dict, *, platform: str, external_id: str | int, generations: int, amount_rubles: int, context: str,
) -> dict:
    """The pack as checkout will charge it (the price list / the buyer's offer decides, not the client), with the
    payment platform, the id for analytics and the menu bonus. 400 when the price is not the current one."""
    if platform == "max":
        if not await max_routes._valid_max_payment_tier(
            pool, user, generations=generations, amount_rubles=amount_rubles, context=context,
        ):
            raise HTTPException(status_code=400, detail="Invalid payment tier")
        telegram_id = None
        payment_platform = max_routes._max_payment_platform(context)
        analytics_id = analytics_user_id("max", external_id)
    else:
        telegram_id = int(external_id)
        generations, amount_rubles = await payments_routes._validate_payment_amount(
            pool,
            payments_routes.PaymentCreateRequest(
                telegram_id=telegram_id, generations=generations, amount_rubles=amount_rubles, context=context,
            ),
        )
        payment_platform = payments_routes._payment_platform(context)
        analytics_id = str(telegram_id)
    bonus_generations = await payments_routes._menu_bonus_generations(pool, context, generations, amount_rubles)
    return {
        "generations": generations, "amount_rubles": amount_rubles, "telegram_id": telegram_id,
        "platform": payment_platform, "analytics_id": analytics_id, "bonus_generations": bonus_generations,
    }


async def _too_many_pending(pool: asyncpg.Pool, user_id: int) -> bool:
    since = datetime.now(timezone.utc) - RECENT_PENDING_WINDOW
    recent = [
        payment for payment in await yookassa_repo.get_all_pending_by_user_id(pool, user_id)
        if payment["created_at"] >= since
    ]
    return len(recent) >= MAX_RECENT_PENDING


async def _store_payment(
    pool: asyncpg.Pool, *, user_id: int, pack: dict, context: str, method: str, flow: str, result: dict,
    confirmation_kind: str, mini_platform: str, operation_key: str | None = None,
) -> tuple[str | None, str | None]:
    """Keep the pending row of a payment just created at the provider and track it; returns (widget token, link).
    502 when the provider gave nothing to pay with (no row: YooKassa expires such a payment)."""
    token = result.get("confirmation_token")
    link = result.get("confirmation_url")
    if not (token if confirmation_kind == "embedded" else link):
        logger.error("Mini App YooKassa create: no %s for %s", confirmation_kind, result["yookassa_id"])
        raise HTTPException(status_code=502, detail="Payment provider returned no confirmation")

    await yookassa_repo.create_payment(
        pool=pool,
        user_id=user_id,
        telegram_id=pack["telegram_id"],
        yookassa_id=result["yookassa_id"],
        amount_rubles=pack["amount_rubles"],
        generations=pack["generations"],
        # The bots' own redirect page is stored like theirs; the wallet buttons and the widget store none.
        confirmation_url=link if method == "redirect" else None,
        platform=pack["platform"],
        operation_key=operation_key,
        offer_slug=payments_routes._offer_slug(context),
        bonus_generations=pack["bonus_generations"],
        provider=result.get("provider", "yookassa"),
        provider_order_id=result.get("order_id"),
    )
    logger.info(
        "Mini App YooKassa payment created: user=%s platform=%s yookassa_id=%s amount=%s gens=%s method=%s confirmation=%s",
        user_id, pack["platform"], result["yookassa_id"], pack["amount_rubles"], pack["generations"], method, confirmation_kind,
    )
    if tbank_acq.is_test_order(result.get("order_id")):
        # Test terminal: no "payment created" event for the funnel; the row itself is settled as test_owner.
        logger.info("Mini App T-Bank TEST payment created: user=%s id=%s", user_id, result["yookassa_id"])
    else:
        await analytics.track(
            "yookassa_payment_created",
            user_id=pack["analytics_id"],
            properties={
                "amount_rubles": pack["amount_rubles"],
                "generations": pack["generations"],
                "bonus_generations": pack["bonus_generations"],
                "yookassa_id": result["yookassa_id"],
                "offer_slug": payments_routes._offer_slug(context),
                "platform": mini_platform,
                "context": context,
                "confirmation": confirmation_kind,
                "method": method,
                "flow": flow,
                "provider": result.get("provider", "yookassa"),
            },
        )
    return token, link


@router.post("/payments/yookassa/create")
async def create_embedded_payment(
    body: MiniAppYookassaCreateRequest,
    miniapp_user: dict = Depends(get_miniapp_user),
    is_preview: bool = Depends(is_miniapp_preview),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """Create a payment for the widget; the buyer is the authenticated Mini App user."""
    if is_preview:
        raise HTTPException(status_code=403, detail="Payments are not available in preview")
    if not payment_gateway.yookassa_configured() and not tbank_acq.is_configured():
        raise HTTPException(status_code=503, detail="YooKassa not configured")

    user = miniapp_user["user"]
    provider = payment_gateway.choose_provider(user["id"])
    if not payment_gateway.configured(provider):
        raise HTTPException(status_code=503, detail="YooKassa not configured")
    identity = miniapp_user["miniapp_identity"]
    is_max = identity["platform"] == "max"

    pack = await _checked_pack(
        pool, user, platform=identity["platform"], external_id=identity["external_id"],
        generations=body.generations, amount_rubles=body.amount_rubles, context=body.context,
    )
    generations, amount_rubles = pack["generations"], pack["amount_rubles"]
    telegram_id, platform, analytics_id = pack["telegram_id"], pack["platform"], pack["analytics_id"]
    bonus_generations = pack["bonus_generations"]
    await analytics.track(
        "miniapp_pack_selected",
        user_id=analytics_id,
        properties={
            "generations": generations,
            "amount": amount_rubles,
            "platform": identity["platform"],
            "context": body.context,
            "source": body.source,
        },
    )

    if await _too_many_pending(pool, user["id"]):
        raise HTTPException(status_code=429, detail="Too many unpaid payments, try again later")

    # Old way (TBANK_TOP_LEVEL, off by default): the form opens at the top level of the WebView and T-Bank brings the
    # buyer back to the page they left from. With the buttons the card form is in an iframe and the wallets are links.
    wallet_button = settings.TBANK_BUTTONS and body.method in tbank_acq.WALLET_METHODS
    return_to = (
        safe_return_to(body.return_to)
        if (settings.TBANK_TOP_LEVEL and provider == payment_gateway.TBANK and not wallet_button) else None
    )
    try:
        result, confirmation_kind = await _create_for_method(
            method=body.method, flow=body.flow, is_max=is_max, amount_rubles=amount_rubles,
            generations=generations, user_id=user["id"], telegram_id=telegram_id, provider=provider,
            return_to=return_to,
        )
    except WalletUnavailable:
        raise HTTPException(status_code=409, detail="method_unavailable")
    except Exception:
        logger.exception("Mini App YooKassa create failed: user=%s method=%s", user["id"], body.method)
        raise HTTPException(status_code=502, detail="Payment provider is unavailable")
    token, link = await _store_payment(
        pool, user_id=user["id"], pack=pack, context=body.context, method=body.method, flow=body.flow,
        result=result, confirmation_kind=confirmation_kind, mini_platform=identity["platform"],
    )
    created_provider = result.get("provider", "yookassa")
    top_level = bool(return_to and created_provider == payment_gateway.TBANK)
    # The card (and the bank's own page) opens in an iframe of the sheet; a wallet link opens outside the app.
    embed = bool(
        body.embed and settings.TBANK_EMBED and created_provider == payment_gateway.TBANK
        and confirmation_kind == "redirect" and not top_level
    )

    return {
        "payment_id": result["yookassa_id"],
        "method": body.method,
        "confirmation_type": confirmation_kind,
        "confirmation_token": token if confirmation_kind == "embedded" else None,
        "confirmation_url": None if confirmation_kind == "embedded" else link,
        "amount": amount_rubles,
        "generations": generations,
        "bonus_generations": bonus_generations,
        "provider": created_provider,
        "embed": embed,
        # The page opens in the same WebView (``window.location``) and returns to the app by itself.
        "top_level": top_level,
    }


def _package_tier(tier: dict) -> dict:
    """A package as the picker shows it: the bots' price list without the Telegram Stars price."""
    return {
        "generations": int(tier["generations"]),
        "rubles": int(tier.get("rubles") or 0),
        "original_rubles": int(tier.get("original_rubles") or 0),
        "discount_percent": int(tier.get("discount_percent") or 0),
        "bonus_generations": int(tier.get("bonus_generations") or 0),
    }


async def _live_special_offer(pool: asyncpg.Pool, user_id: int) -> dict | None:
    """The discount offer this user holds right now (win-back, H3 ladder), the same rules as checkout."""
    for slug in (*discount_ladder.WINBACK_OFFERS, *discount_ladder.OFFERS):
        offer = await discount_ladder.live_offer(pool, user_id, slug)
        if offer is None:
            continue
        row = await discount_ladder_repo.get_offer(pool, user_id, slug)
        if row is None:
            continue
        return {
            "slug": slug,
            "context": discount_ladder.context_for(slug),
            "expires_at": row["expires_at"].isoformat(),
            "tiers": [_package_tier(tier) for tier in discount_ladder.tier_dicts(offer)],
        }
    return None


async def _tbank_pay_methods(provider: str) -> dict | None:
    """The buttons of the sheet for a T-Bank payment: the wallets the bank gives a link for (SBP first), the card
    always, and, when no wallet is open, one button to the bank's own page (it has SBP/T-Pay; a link method is off
    until the bank enables it for the terminal). None = the single «Оплатить» button (YooKassa, or the buttons are off)."""
    if provider != payment_gateway.TBANK or not settings.TBANK_BUTTONS:
        return None
    wallets = await tbank_methods.available_wallets()
    return {"wallets": wallets, "card": True, "bank_page": not wallets}


@router.get("/payments/packages")
async def get_packages(
    response: Response,
    miniapp_user: dict = Depends(get_miniapp_user),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """The packages the bot's "buy energy" menu shows this user, for the picker inside the Mini App.

    ``tiers`` is ``pricing_repo.get_menu_tiers`` — the very call both bots make for their menu and the
    one checkout validates the price against — so the list and the charge cannot drift apart.
    ``offer`` is a live discount (win-back / H3) with its own checkout context. With the flag off the
    client keeps sending the buyer to the bot (``enabled: false``).
    """
    response.headers["Cache-Control"] = "no-store"
    if not settings.MINIAPP_PAYWALL_INAPP:
        return {"enabled": False, "tiers": [], "offer": None}
    tiers = [_package_tier(tier) for tier in await pricing_repo.get_menu_tiers(pool)]
    user_id = miniapp_user["user"]["id"]
    try:
        provider = payment_gateway.choose_provider(user_id)
    except HTTPException:
        # T-Bank test mode and not a test account: the sheet is shown, the payment itself is refused.
        provider = payment_gateway.YOOKASSA
    return {
        "enabled": True,
        "tiers": [tier for tier in tiers if tier["rubles"] > 0],
        "offer": await _live_special_offer(pool, user_id),
        # Where the payment will go (the sheet's caption) and whether it is the test terminal (no money moves).
        "provider": provider,
        "test_mode": bool(settings.TBANK_TEST_MODE and provider == payment_gateway.TBANK),
        "pay_methods": await _tbank_pay_methods(provider),
    }


class PaywallOpened(BaseModel):
    source: str = Field(max_length=32, pattern=r"^[a-z0-9_]+$")


@router.post("/paywall-opened", status_code=204)
async def post_paywall_opened(
    body: PaywallOpened,
    miniapp_user: dict = Depends(get_miniapp_user),
    is_preview: bool = Depends(is_miniapp_preview),
) -> Response:
    """Analytics: the package picker was shown (the bots track their own ``paywall_shown``)."""
    identity = miniapp_user["miniapp_identity"]
    if not is_preview:
        await analytics.track(
            "miniapp_paywall_opened",
            user_id=analytics_user_id(identity["platform"], identity["external_id"]),
            properties={"platform": "max" if identity["platform"] == "max" else "tg", "source": body.source},
        )
    return Response(status_code=204)


_RETURN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>YOUR_BRAND — оплата</title>
<style>html,body{{height:100%;margin:0}}body{{display:flex;align-items:center;justify-content:center;font:16px/1.4 system-ui,sans-serif;
background:#fff;color:#1a1a1a;text-align:center;padding:24px;box-sizing:border-box}}a{{display:inline-block;margin-top:16px;padding:12px 20px;
border-radius:12px;background:#1a1a1a;color:#fff;text-decoration:none}}</style></head><body><div>
<p><b>{title}</b></p><p>{text}</p><a id="back" href="{back}" style="display:none">Вернуться в YOUR_BRAND</a></div>
<script>
(function () {{
  var ok = {ok};
  var to = {to};
  var show = function () {{ document.getElementById("back").style.display = "inline-block"; }};
  if (window.parent !== window) {{
    window.parent.postMessage({{ type: "app-pay-return", ok: ok }}, "*");
  }} else if (to) {{
    // The form was open at the top level of the Mini App's WebView: back to the page it left, which checks the payment.
    window.location.replace(to + "?pay=return&ok=" + (ok ? "1" : "0"));
    window.setTimeout(show, 3000);
  }} else {{
    show();
  }}
}})();
</script></body></html>"""


@router.get("/pay-return", include_in_schema=False)
async def pay_return(
    p: Literal["max", "tg"] = "tg", ok: Literal["0", "1"] = "1", to: str | None = None,
) -> HTMLResponse:
    """The page T-Bank sends the buyer to after the form (SuccessURL/FailURL), public and stateless.

    Inside an iframe it tells the parent (``postMessage``) that the form is over. At the top level, with ``to`` (the
    Mini App page the buyer left, checked against our own origins), it sends the buyer straight back there; the app
    then checks the payment status itself, so nothing here is trusted. Without ``to`` (opened in a browser) it just
    offers a way back into the app.
    """
    success = ok == "1"
    back = (
        f"https://max.ru/{settings.MAX_BOT_USERNAME}?startapp=pay_return" if p == "max"
        else f"https://t.me/{settings.BOT_USERNAME}?startapp=pay_return"
    )
    target = safe_return_to(to)
    html = _RETURN_PAGE.format(
        title="Оплата принята" if success else "Оплата не прошла",
        text="Возвращаемся в приложение…" if success else "Деньги не списаны. Вернись в приложение и попробуй ещё раз.",
        back=back,
        ok="true" if success else "false",
        to=json.dumps(target) if target else "null",
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


_OPEN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>Оплата SberPay</title>
<style>html,body{{height:100%;margin:0}}body{{display:flex;align-items:center;justify-content:center;font:16px/1.45 system-ui,sans-serif;
background:#fff;color:#1a1a1a;text-align:center;padding:24px;box-sizing:border-box}}.b{{display:block;margin:12px auto 0;padding:15px 20px;max-width:320px;
border-radius:14px;text-decoration:none;font-weight:600;border:0;width:100%;box-sizing:border-box;font-size:16px;font-family:inherit}}.g{{background:#21A038;color:#fff}}
.l{{background:#f1f1f1;color:#1a1a1a}}small{{color:#666}}#more{{display:none}}</style></head>
<body><div><p><b>Оплата через SberPay</b></p><p>Нажми, чтобы открыть приложение СберБанк Онлайн и подтвердить оплату.</p>
<a class="b g" id="go" href="{app}">Открыть Сбер</a><a class="b l" href="{back}">Вернуться в YOUR_BRAND</a>
<div id="more"><p><small>Не открылось? Проверь, что приложение Сбера установлено.</small></p>
<button class="b l" id="alt" type="button">Попробовать другой способ</button>
<a class="b l" href="https://www.sberbank.ru/ru/person/payments/online_sberpay">О SberPay и установка приложения</a></div></div>
<script>
(function () {{
  var app = {app_js};
  var id = (app.match(/bankInvoiceId=([A-Za-z0-9]+)/) || [])[1] || "";
  // Схемы приложения Сбера на iOS в порядке из руководства Сбера (mWeb2app): на айфоне ссылка банка может не сработать.
  var alt = ["onlineapp", "onlineios-app", "startonline", "onlineappmobile", "budgetonline-ios", "ios-app-smartonline", "btripsexpenses", "kidsonline"];
  var next = 0;
  window.setTimeout(function () {{ window.location.href = app; }}, 300);
  window.setTimeout(function () {{
    if (document.hidden) return;
    document.getElementById("more").style.display = "block";
    if (!/iPhone|iPad|iPod/.test(navigator.userAgent) || !id) document.getElementById("alt").style.display = "none";
  }}, 2200);
  document.getElementById("alt").addEventListener("click", function () {{
    var scheme = alt[next++ % alt.length];
    window.location.href = scheme + "://sbolpay/invoicing/v2?bankInvoiceId=" + id;
  }});
}})();
</script></body></html>"""


@router.get("/pay-open", include_in_schema=False)
async def pay_open(u: str, p: Literal["max", "tg"] = "tg") -> HTMLResponse:
    """Opens the bank app from the external browser (the Mini App hands this page to ``openLink``).

    ``u`` is the bank's custom-scheme link; it is passed only when it has exactly the shape the bank gives for SberPay
    (so the page is never a redirect to a foreign scheme). The button is the reliable way (a tap), the timer is a try
    without one; ``Вернуться`` goes back to the Mini App, which checks the payment itself.
    """
    if not _APP_SCHEME_LINK.match(u or ""):
        raise HTTPException(status_code=400, detail="Unsupported link")
    back = (
        f"https://max.ru/{settings.MAX_BOT_USERNAME}?startapp=pay_return" if p == "max"
        else f"https://t.me/{settings.BOT_USERNAME}?startapp=pay_return"
    )
    html = _OPEN_PAGE.format(app=html_escape(u, quote=True), app_js=json.dumps(u), back=html_escape(back, quote=True))
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


class PayTicketRequest(BaseModel):
    generations: int
    amount_rubles: int
    context: str = "menu"
    source: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_]+$")


@router.post("/payments/ticket")
async def issue_pay_ticket(
    body: PayTicketRequest,
    miniapp_user: dict = Depends(get_miniapp_user),
    is_preview: bool = Depends(is_miniapp_preview),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """A ticket for the wallet buttons (see ``pay_ticket``): the price is checked now and once more when ``pay-go``
    makes the payment. Creates nothing at the bank and stores nothing."""
    if is_preview:
        raise HTTPException(status_code=403, detail="Payments are not available in preview")
    user = miniapp_user["user"]
    if payment_gateway.choose_provider(user["id"]) != payment_gateway.TBANK or not settings.TBANK_BUTTONS:
        raise HTTPException(status_code=409, detail="tickets_not_used")
    identity = miniapp_user["miniapp_identity"]
    pack = await _checked_pack(
        pool, user, platform=identity["platform"], external_id=identity["external_id"],
        generations=body.generations, amount_rubles=body.amount_rubles, context=body.context,
    )
    ticket, expires_at = pay_ticket.issue(
        user_id=user["id"], platform=identity["platform"], external_id=identity["external_id"],
        generations=pack["generations"], amount_rubles=pack["amount_rubles"], context=body.context, source=body.source,
    )
    return {"ticket": ticket, "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat()}


_GO_ERROR_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>YOUR_BRAND — оплата</title>
<style>html,body{{height:100%;margin:0}}body{{display:flex;align-items:center;justify-content:center;font:16px/1.45 system-ui,sans-serif;
background:#fff;color:#1a1a1a;text-align:center;padding:24px;box-sizing:border-box}}a{{display:block;margin:18px auto 0;padding:15px 20px;max-width:320px;
border-radius:14px;background:#1a1a1a;color:#fff;text-decoration:none;font-weight:600}}</style></head><body><div>
<p><b>Не получилось открыть оплату</b></p><p>{text}</p><a href="{back}">{label}</a></div></body></html>"""


def _go_error(text: str, p: str, status: int, back: str | None = None, label: str = "Вернуться в YOUR_BRAND") -> HTMLResponse:
    back = back or (
        f"https://max.ru/{settings.MAX_BOT_USERNAME}?startapp=pay_return" if p == "max"
        else f"https://t.me/{settings.BOT_USERNAME}?startapp=pay_return"
    )
    return HTMLResponse(
        _GO_ERROR_PAGE.format(text=html_escape(text), back=html_escape(back, quote=True), label=html_escape(label)),
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/pay-go", include_in_schema=False)
async def pay_go(
    request: Request, t: str, m: str, p: Literal["max", "tg"] = "tg", pool: asyncpg.Pool = Depends(get_db),
):
    """The page a wallet button opens (``openLink`` straight from the tap, so the gesture is not lost): makes the
    payment of the ticket ``t`` for the way ``m`` (sberpay | sbp | tpay | page) and redirects (302) to the bank's link.

    Public: the signed ticket is the credential. Idempotent per (ticket, way): a second tap, or a reload of this page,
    finds the payment made by the first and only asks the bank for the link again. Errors are a plain page with a way back.
    """
    ticket = pay_ticket.verify(t)
    way = pay_ticket.WAYS.get(m)
    if ticket is None or way is None:
        return _go_error("Ссылка устарела. Вернись в YOUR_BRAND и выбери способ оплаты ещё раз.", p, 410)
    p = "max" if ticket.platform == "max" else "tg"
    is_max = p == "max"
    try:
        user_data = await users_repo.get_user_by_id(pool, ticket.user_id)
        user = user_data["user"] if user_data else None
        if user is None:
            return _go_error("Не нашли твой аккаунт. Открой YOUR_BRAND из бота.", p, 403)
        if payment_gateway.choose_provider(user["id"]) != payment_gateway.TBANK or not settings.TBANK_BUTTONS:
            return _go_error("Этот способ сейчас недоступен. Вернись в YOUR_BRAND и выбери другой.", p, 409)
        try:
            pack = await _checked_pack(
                pool, user, platform=ticket.platform, external_id=ticket.external_id,
                generations=ticket.generations, amount_rubles=ticket.amount_rubles, context=ticket.context,
            )
        except HTTPException:
            return _go_error("Цены обновились. Вернись в YOUR_BRAND и выбери пакет ещё раз.", p, 400)
        key = ticket.operation_key(m)
        existing = await yookassa_repo.get_by_operation_key(
            pool, user_id=user["id"], platform=pack["platform"], operation_key=key,
        )
        if existing is None:
            if await _too_many_pending(pool, user["id"]):
                return _go_error("Слишком много неоплаченных попыток. Подожди пару минут.", p, 429)
            await analytics.track(
                "miniapp_pack_selected",
                user_id=pack["analytics_id"],
                properties={
                    "generations": pack["generations"], "amount": pack["amount_rubles"], "platform": ticket.platform,
                    "context": ticket.context, "source": ticket.source,
                },
            )
            result, kind = await _create_for_method(
                method=way, flow="app" if _MOBILE_UA.search(request.headers.get("user-agent", "")) else "web",
                is_max=is_max, amount_rubles=pack["amount_rubles"], generations=pack["generations"], user_id=user["id"],
                telegram_id=pack["telegram_id"], provider=payment_gateway.TBANK,
            )
            try:
                await _store_payment(
                    pool, user_id=user["id"], pack=pack, context=ticket.context, method=way, flow="app",
                    result=result, confirmation_kind=kind, mini_platform=ticket.platform, operation_key=key,
                )
            except asyncpg.UniqueViolationError:
                # A double tap: the other request stored its payment first; ours is closed at the bank and its twin is used.
                try:
                    await tbank_acq.cancel_payment(result["yookassa_id"])
                except tbank_acq.TBankError:
                    logger.warning("pay-go: could not cancel the twin payment %s", result["yookassa_id"], exc_info=True)
                existing = await yookassa_repo.get_by_operation_key(
                    pool, user_id=user["id"], platform=pack["platform"], operation_key=key,
                )
            else:
                link = result.get("confirmation_url")
                return _go_redirect(link, p)
        if existing["status"] in ("succeeded", "test_owner"):
            return _go_error("Эта оплата уже прошла. Вернись в YOUR_BRAND — энергия начислена.", p, 200)
        if existing["status"] != "pending":
            return _go_error("Этот платёж закрыт. Вернись в YOUR_BRAND и выбери способ оплаты ещё раз.", p, 410)
        if way == "redirect":
            link = existing.get("confirmation_url")
        else:
            link = pay_open_url(await tbank_acq.wallet_link(way, existing["yookassa_id"]), is_max)
        return _go_redirect(link, p)
    except WalletUnavailable:
        return _go_error("Этот способ сейчас недоступен. Вернись в YOUR_BRAND и выбери другой.", p, 409)
    except HTTPException as exc:
        return _go_error("Платёжная система не ответила. Вернись в YOUR_BRAND и попробуй ещё раз.", p, exc.status_code)
    except Exception:
        logger.exception("pay-go failed: ticket user=%s way=%s", ticket.user_id, m)
        return _go_error("Платёжная система не ответила. Вернись в YOUR_BRAND и попробуй ещё раз.", p, 502)


def _go_redirect(link: str | None, p: str):
    if not link or not link.lower().startswith("https://"):
        return _go_error("Банк не дал ссылку для оплаты. Вернись в YOUR_BRAND и выбери другой способ.", p, 502)
    return RedirectResponse(link, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})



# ── buttons of the chat bots: the payment is made by the bot's create call, these pages only lead to the bank ──

def _bot_chat_url(p: str) -> str:
    return f"https://max.ru/{settings.MAX_BOT_USERNAME}" if p == "max" else f"https://t.me/{settings.BOT_USERNAME}"


def _bot_error(text: str, p: str, status: int) -> HTMLResponse:
    return _go_error(text, p, status, back=_bot_chat_url(p))


def _logo_uri(name: str) -> str:
    svg = (Path(__file__).resolve().parent.parent.parent / "static" / "pay" / f"{name}.svg").read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode()


_LOGOS = {name: _logo_uri(name) for name in ("sberpay", "sbp", "tpay")}

_CHOOSE_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>YOUR_BRAND — оплата</title>
<style>html,body{margin:0}body{font:16px/1.45 system-ui,sans-serif;background:#fff;color:#1a1a1a;text-align:center;padding:28px 20px 40px;
box-sizing:border-box;min-height:100vh}.w{max-width:360px;margin:0 auto}h1{font-size:20px;margin:0 0 6px}.s{color:#666;margin:0 0 22px}
.b{display:flex;align-items:center;justify-content:center;gap:10px;height:52px;margin:0 0 12px;border-radius:14px;text-decoration:none;
font-weight:700;font-size:16px;box-sizing:border-box;border:1px solid transparent}.b img{height:26px;width:auto;display:block}
.sber{background:#21A038;color:#fff}.sbp{background:#fff;color:#1D1346;border-color:#d9d9e0}.tpay{background:#FFDD2D;color:#1C1C1E}
.card{background:#f1f1f1;color:#1a1a1a}.tpay img{height:22px}small{display:block;color:#888;margin-top:18px;font-size:13px}</style></head>
<body><div class="w"><h1>Выбери способ оплаты</h1><p class="s">{{TITLE}}</p>{{BUTTONS}}
<small>Оплата проходит на стороне Т-Банка. Деньги спишутся только после подтверждения.</small></div></body></html>"""


def _choose_button(key: str, way: str, p: str, amount: int) -> str:
    href = html_escape(bot_pay.button_url(key, way, p), quote=True)
    if way == "sberpay":
        return f'<a class="b sber" href="{href}">Оплатить {amount} ₽<img alt="SberPay" src="{_LOGOS["sberpay"]}"></a>'
    if way == "sbp":
        return f'<a class="b sbp" href="{href}"><img alt="" src="{_LOGOS["sbp"]}">СБП</a>'
    if way == "tpay":
        return f'<a class="b tpay" href="{href}"><img alt="T-Pay" src="{_LOGOS["tpay"]}"></a>'
    return f'<a class="b card" href="{href}">Картой</a>'


async def _bot_payment(pool: asyncpg.Pool, link: "bot_pay.BotLink") -> dict | None:
    """The payment a bot key opens: it must be a T-Bank payment of that very buyer (anything else looks like «not found»)."""
    payment = await yookassa_repo.get_by_yookassa_id(pool, link.payment_id)
    if (
        payment is None or payment["user_id"] != link.user_id
        or payment_gateway.provider_of(payment) != payment_gateway.TBANK
    ):
        return None
    return payment


def _closed_payment_page(payment: dict, p: str) -> HTMLResponse | None:
    """The page for a payment that cannot be paid any more (None while it is open)."""
    if payment["status"] in ("succeeded", "test_owner"):
        return _bot_error("Эта оплата уже прошла — энергия начислена. Вернись в бота.", p, 200)
    if payment["status"] != "pending":
        return _bot_error("Этот платёж закрыт. Вернись в бота и выбери пакет ещё раз.", p, 410)
    return None


@router.get("/pay-choose", include_in_schema=False)
async def pay_choose(k: str, p: str = "tg", pool: asyncpg.Pool = Depends(get_db)):
    """The choice of the way to pay for a payment the bot made: the page an old bot (one that knows nothing of
    ``pay_buttons``) opens from its «Оплатить», and the way back from a button that did not work. Public: the signed key is
    the credential, the page shows the price and four links to ``pay-bot``."""
    link = bot_pay.verify(k)
    if link is None:
        return _bot_error("Ссылка устарела. Вернись в бота и выбери пакет ещё раз.", p if p in bot_pay.PLATFORMS else "tg", 410)
    p = link.platform
    payment = await _bot_payment(pool, link)
    if payment is None:
        return _bot_error("Не нашли этот платёж. Вернись в бота и выбери пакет ещё раз.", p, 404)
    closed = _closed_payment_page(payment, p)
    if closed is not None:
        return closed
    ways = [*await bot_pay.open_wallet_ways(), bot_pay.CARD_BUTTON[0]]
    amount = int(payment["amount_rubles"])
    html = _CHOOSE_PAGE.replace("{{TITLE}}", html_escape(f"{int(payment['generations'])} ⚡ энергии · {amount} ₽")).replace(
        "{{BUTTONS}}", "".join(_choose_button(k, way, p, amount) for way in ways),
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/pay-bot", include_in_schema=False)
async def pay_bot(k: str, m: str, p: str = "tg", pool: asyncpg.Pool = Depends(get_db)):
    """The page a button of a bot opens: finds the payment the bot already made and sends (302) to the bank — the link of
    the wallet (``sberpay`` | ``sbp`` | ``tpay``; asked from the bank at the tap) or T-Bank's own form (``card``).

    Public: the signed key is the credential. Nothing is created here, so a second tap or a reload only asks the bank
    for the link again; the payment the bot polls stays the one that is paid. A closed, paid or foreign payment is a plain
    page with a way back to the chat."""
    link = bot_pay.verify(k)
    if link is None:
        return _bot_error("Ссылка устарела. Вернись в бота и выбери пакет ещё раз.", p if p in bot_pay.PLATFORMS else "tg", 410)
    p = link.platform
    if m not in bot_pay.WAYS:
        return _bot_error("Такого способа оплаты нет. Вернись в бота и выбери пакет ещё раз.", p, 400)
    try:
        payment = await _bot_payment(pool, link)
        if payment is None:
            return _bot_error("Не нашли этот платёж. Вернись в бота и выбери пакет ещё раз.", p, 404)
        closed = _closed_payment_page(payment, p)
        if closed is not None:
            return closed
        if m == "card":
            target = payment.get("confirmation_url")
        else:
            if not settings.TBANK_BUTTONS:
                return _bot_error("Этот способ сейчас недоступен. Вернись в бота и выбери пакет ещё раз.", p, 409)
            try:
                wallet = await tbank_acq.wallet_link(bot_pay.WAYS[m], payment["yookassa_id"])
            except tbank_acq.TBankError as exc:
                logger.warning("pay-bot: %s link refused: payment=%s code=%s", m, payment["yookassa_id"], exc.code)
                if exc.code:
                    tbank_methods.mark_unavailable(bot_pay.WAYS[m])
                    return _go_error(
                        "Этот способ сейчас недоступен. Вернись назад и выбери другой.", p, 409,
                        back=bot_pay.choose_url(k, p), label="Выбрать другой способ",
                    )
                return _bot_error("Платёжная система не ответила. Вернись в бота и попробуй ещё раз.", p, 502)
            target = pay_open_url(wallet, p == "max")
        logger.info("pay-bot: payment=%s user=%s platform=%s way=%s", payment["yookassa_id"], link.user_id, p, m)
        return _go_redirect(target, p)
    except HTTPException as exc:
        return _bot_error("Платёжная система не ответила. Вернись в бота и попробуй ещё раз.", p, exc.status_code)
    except Exception:
        logger.exception("pay-bot failed: payment=%s way=%s", link.payment_id, m)
        return _bot_error("Платёжная система не ответила. Вернись в бота и попробуй ещё раз.", p, 502)


_MOBILE_UA = re.compile(r"Android|iPhone|iPad|iPod", re.I)


async def _payment_state(request: Request, pool: asyncpg.Pool, user: dict, payment: dict) -> dict:
    """Status of a payment of ``user``, settling it when the provider says it is paid.

    The webhook normally settles first; polling covers a late or lost one. The settlement is the bots' own
    (``_process_succeeded_payment``), so a payment credited twice is impossible whichever path gets there first.
    """
    payment_id = payment["yookassa_id"]
    credited = None
    # T-Bank closes a payment as failed on REJECTED, but the buyer can retry on the same form (another card): a
    # recently closed T-Bank payment is read again, so a retry that succeeds is credited (and reopened) here too.
    retry_window = (
        payment_gateway.provider_of(payment) == payment_gateway.TBANK
        and payment["status"] == "canceled"
        and payment.get("completed_at") is not None
        and datetime.now(timezone.utc) - payment["completed_at"] < RECENT_PENDING_WINDOW
    )
    if payment["status"] == "pending" or retry_window:
        try:
            remote = await payment_gateway.get_payment_status(payment)
        except Exception:
            logger.warning("Mini App payment poll: provider unavailable for %s", payment_id, exc_info=True)
            remote = {}
        if remote.get("status") == "succeeded" and payment_gateway.matches_local_payment(remote, payment):
            if retry_window:
                await yookassa_repo.reopen_canceled(pool, payment_id)
            await yookassa_repo.save_payment_method(pool, payment_id, remote.get("payment_method"))
            paid = await payments_routes._process_succeeded_payment(request, pool, payment_id, source="poll")
            if paid:
                credited = paid.get("credited_generations", paid["generations"])
        elif remote.get("status") == "canceled" and not retry_window:
            await yookassa_repo.mark_canceled(pool, payment_id)
        payment = await yookassa_repo.get_by_yookassa_id(pool, payment_id)

    # A test-terminal payment is stored as test_owner; the client sees it as an ordinary paid one.
    status = "succeeded" if payment["status"] == "test_owner" else payment["status"]
    response = {
        "status": status,
        "generations": payment["generations"],
        "amount": payment["amount_rubles"],
    }
    if status == "succeeded":
        user_data = await users_repo.get_user_by_id(pool, user["id"])
        response["credited_generations"] = (
            credited if credited is not None else payment["generations"] + int(payment["bonus_generations"] or 0)
        )
        response["balance"] = user_data["generations_remaining"] if user_data else None
    return response


@router.get("/payments/ticket/{ticket}")
async def get_ticket_status(
    ticket: str,
    request: Request,
    miniapp_user: dict = Depends(get_miniapp_user),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """Status of the payments a ticket started (the buyer pays outside the app, the sheet follows the ticket).

    ``pending`` while there is no payment yet (the buyer has not reached ``pay-go``) or one is still open; ``succeeded``
    as soon as any of them is paid (a buyer who tried SberPay and then SBP pays only one, but either may be it);
    ``canceled`` when every payment of the ticket is closed.
    """
    user = miniapp_user["user"]
    parsed = pay_ticket.verify(ticket, leeway=pay_ticket.STATUS_LEEWAY_SECONDS)
    if parsed is None or parsed.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Ticket not found")
    payments = await yookassa_repo.list_by_operation_prefix(pool, user_id=user["id"], prefix=parsed.operation_prefix)
    if not payments:
        return {"status": "pending", "generations": parsed.generations, "amount": parsed.amount_rubles}
    states = [await _payment_state(request, pool, user, payment) for payment in payments]
    for state in states:
        if state["status"] == "succeeded":
            return state
    for state in states:
        if state["status"] != "canceled":
            return state
    return states[0]


@router.get("/payments/{payment_id}")
async def get_payment_status(
    payment_id: str,
    request: Request,
    miniapp_user: dict = Depends(get_miniapp_user),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """Status of the caller's own payment, settling it when the provider says it is paid."""
    user = miniapp_user["user"]
    payment = await yookassa_repo.get_by_yookassa_id(pool, payment_id)
    if payment is None or payment["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Payment not found")
    return await _payment_state(request, pool, user, payment)
