import asyncio  # --- T-BANK ---
import logging
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from app.api.deps import AuthDep, get_db
from app.db import photos_repo, users_repo, pricing_repo, referrals_repo, robokassa_repo, yookassa_repo, stars_repo, lava_repo
from app.services.analytics import analytics, analytics_user_id
from app.services.notifications_client import (
    fire_event_bg,
    cancel_event,
    notifications_client_for,
    notify_payment_canceled,
    notify_payment_succeeded,
)
from app.services import yookassa as yookassa_service
from app.services import bot_pay, payment_gateway, tbank_acq  # --- T-BANK ---
from app.services import robokassa as robokassa_service
from app.services import lava as lava_service
from app.services import max_bot
from app.services import first_purchase_bonus
from app.services.referral_notify import notify_referrer
from app.services import discount_ladder, gift_push
from app.services import photo_rating as photo_rating_service
from app.core.config import settings
import asyncpg

logger = logging.getLogger(__name__)
router = APIRouter()


# Legacy deep links remain valid for notifications sent before the new
# first-purchase bonus offer was launched.
FIRST_PURCHASE_OFFER_GENERATIONS = frozenset({10})

# Contexts that check out the standing menu packages (the only ones the menu bonus applies to).
MENU_CONTEXTS = (None, "", "legacy", "menu")


class TierResponse(BaseModel):
    generations: int
    bonus_generations: int = 0
    stars: int = 0
    rubles: int = 0
    original_rubles: int = 0
    discount_percent: int = 0


class PriceResponse(BaseModel):
    tiers: list[TierResponse]
    # Set when the tiers are a live discount offer (``discount:<slug>`` context); None means the
    # offer is unavailable and the tiers are the ordinary packages.
    offer_slug: str | None = None
    # Energy an invited user gets on top of the first purchase.
    referral_friend_bonus: int = 0


async def _get_first_purchase_offer_tiers(pool: asyncpg.Pool) -> list[dict]:
    """Return the ordinary 10- and 20-generation packages for onboarding."""
    tiers = await pricing_repo.get_tiers(pool)
    offer_tiers = [
        dict(tier)
        for tier in tiers
        if int(tier.get("generations") or 0) in FIRST_PURCHASE_OFFER_GENERATIONS
        and int(tier.get("rubles") or 0) > 0
    ]
    return sorted(offer_tiers, key=lambda tier: int(tier["generations"]))


async def _get_payment_offer_user(pool: asyncpg.Pool, telegram_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payment_offered_at, has_purchased FROM users WHERE telegram_id = $1",
            telegram_id,
        )
    return dict(row) if row else None


async def _matches_onboarding_bonus_tier(
    pool: asyncpg.Pool,
    telegram_id: int,
    generations: int,
    amount_rubles: int,
) -> bool:
    user = await _get_payment_offer_user(pool, telegram_id)
    if not user or not first_purchase_bonus.bonus_window_open(user):
        return False
    return any(
        int(tier["generations"]) == generations and int(tier["rubles"]) == amount_rubles
        for tier in await pricing_repo.get_first_purchase_bonus_tiers(pool)
    )


def _payment_platform(context: str | None) -> str:
    # Only the delayed bonus checkout credits the extra 50% on settlement; the
    # initial post-result offer is an ordinary purchase.
    return "telegram_bonus" if context == "onboarding_bonus" else "telegram"


async def _get_expected_menu_tier(pool: asyncpg.Pool, generations: int) -> dict | None:
    tiers = await pricing_repo.get_menu_tiers(pool)
    return next((tier for tier in tiers if tier["generations"] == generations), None)


async def _menu_bonus_generations(
    pool: asyncpg.Pool, context: str | None, generations: int, amount_rubles: int,
) -> int:
    """Bonus energy the menu promo adds to this checkout (0 outside the menu).

    Only the menu pays it: the first-purchase +50% (``onboarding_bonus``), the
    onboarding paywall and the discount offers never carry a menu bonus, so the
    two bonuses cannot stack.
    """
    if context not in MENU_CONTEXTS:
        return 0
    tier = await _get_expected_menu_tier(pool, generations)
    if not tier or int(tier.get("rubles") or 0) != amount_rubles:
        return 0
    return int(tier.get("bonus_generations") or 0)


async def _matches_menu_tier(pool: asyncpg.Pool, generations: int, amount_rubles: int) -> bool:
    tier = await _get_expected_menu_tier(pool, generations)
    return bool(tier and int(tier.get("rubles") or 0) == amount_rubles)


async def _matches_onboarding_tier(
    pool: asyncpg.Pool,
    telegram_id: int,
    generations: int,
    amount_rubles: int,
) -> bool:
    offer_tiers = await _get_first_purchase_offer_tiers(pool)
    tier = next((item for item in offer_tiers if item["generations"] == generations), None)
    if tier is None or int(tier["rubles"]) != amount_rubles:
        return False

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payment_offered_at, has_purchased FROM users WHERE telegram_id = $1",
            telegram_id,
        )
    return bool(row and row["payment_offered_at"] and not row["has_purchased"])


async def _get_onboarding_ladder_offer(pool: asyncpg.Pool, telegram_id: int) -> tuple[int, int]:
    raise HTTPException(status_code=410, detail="Discount offers are unavailable")


async def _get_onboarding_push_offer(
    pool: asyncpg.Pool,
    telegram_id: int,
    offer_code: str | None,
) -> tuple[int, int]:
    """Resolve a push deep link to the package and price it must charge."""
    raise HTTPException(status_code=410, detail="Discount offers are unavailable")


async def _validate_discount_offer(pool: asyncpg.Pool, body: "PaymentCreateRequest") -> tuple[int, int]:
    """A discounted price is honoured only for the delivered, unexpired push of the user's own group.

    The price comes from the server's offer table, never from the client: the requested package and
    amount must be exactly one of the offer's tiers.
    """
    unavailable = HTTPException(status_code=400, detail="Discount offer is unavailable")
    offer = discount_ladder.offer_from_context(body.context)
    user = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if offer is None or not user:
        raise unavailable
    if await discount_ladder.live_offer(pool, user["user"]["id"], offer.slug) is None:
        raise unavailable
    if not discount_ladder.matches(offer, body.generations, body.amount_rubles):
        raise unavailable
    return body.generations, body.amount_rubles


def _offer_slug(context: str | None) -> str | None:
    offer = discount_ladder.offer_from_context(context)
    return offer.slug if offer else None


async def _validate_payment_amount(pool: asyncpg.Pool, body: "PaymentCreateRequest") -> tuple[int, int]:
    context = body.context

    if context in (None, "", "legacy"):
        if await _matches_menu_tier(pool, body.generations, body.amount_rubles):
            return body.generations, body.amount_rubles
        if await _matches_onboarding_tier(pool, body.telegram_id, body.generations, body.amount_rubles):
            return body.generations, body.amount_rubles
        raise HTTPException(status_code=400, detail="Invalid payment tier")

    if context == "menu":
        if not await _matches_menu_tier(pool, body.generations, body.amount_rubles):
            raise HTTPException(status_code=400, detail="Invalid payment tier")
        return body.generations, body.amount_rubles

    if context == "onboarding_paywall":
        if not await _matches_onboarding_tier(pool, body.telegram_id, body.generations, body.amount_rubles):
            raise HTTPException(status_code=400, detail="Invalid payment tier")
        return body.generations, body.amount_rubles

    if context == "onboarding_bonus":
        if not await _matches_onboarding_bonus_tier(pool, body.telegram_id, body.generations, body.amount_rubles):
            raise HTTPException(status_code=400, detail="Onboarding bonus is unavailable")
        return body.generations, body.amount_rubles

    if context == "onboarding_trial":
        if body.generations != 10 or not await _matches_onboarding_tier(
            pool, body.telegram_id, body.generations, body.amount_rubles,
        ):
            raise HTTPException(status_code=400, detail="Invalid onboarding trial tier")
        return body.generations, body.amount_rubles

    if context == "special_offer":
        return await _get_onboarding_push_offer(pool, body.telegram_id, body.offer_code)

    if context and context.startswith(discount_ladder.CONTEXT_PREFIX):
        return await _validate_discount_offer(pool, body)

    raise HTTPException(status_code=400, detail="Invalid payment context")


@router.get("/price", response_model=PriceResponse, dependencies=[AuthDep])
async def get_price(
    telegram_id: int,
    context: str = "menu",
    offer_code: str | None = None,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Get pricing tiers for the requested payment flow."""
    result = await _price_tiers(telegram_id, context, offer_code, pool)
    result.referral_friend_bonus = await referrals_repo.pending_friend_bonus(pool, telegram_id=telegram_id)
    return result


async def _price_tiers(
    telegram_id: int, context: str, offer_code: str | None, pool: asyncpg.Pool,
) -> PriceResponse:
    markup = await pricing_repo.get_stars_markup_percent(pool)

    def _tier(data: dict) -> TierResponse:
        # The legacy ``stars`` baked into the tiers JSON is stale; the star price
        # is always derived from the live ruble price plus the configured markup.
        tier = TierResponse(**data)
        tier.stars = pricing_repo.rubles_to_stars(tier.rubles, markup)
        return tier

    if context == "special_offer":
        generations, rubles = await _get_onboarding_push_offer(pool, telegram_id, offer_code)
        return PriceResponse(tiers=[_tier({"generations": generations, "rubles": rubles})])

    if context.startswith(discount_ladder.CONTEXT_PREFIX):
        # An unknown, expired or foreign offer shows the ordinary packages (offer_slug stays empty)
        # so an old push never promises a price that checkout would refuse.
        offer = discount_ladder.offer_from_context(context)
        user = await users_repo.get_user_by_telegram_id(pool, telegram_id)
        if offer is not None and user and await discount_ladder.live_offer(pool, user["user"]["id"], offer.slug):
            return PriceResponse(tiers=[_tier(t) for t in discount_ladder.tier_dicts(offer)], offer_slug=offer.slug)
        tiers = await pricing_repo.get_menu_tiers(pool, bonus=False)
        return PriceResponse(tiers=[_tier(t) for t in tiers])

    if context == "onboarding_bonus":
        # Outside the 2h..26h window (too early, expired, already paid) fall
        # through to the ordinary packages so an old push never shows a
        # bonus that checkout would refuse.
        user = await _get_payment_offer_user(pool, telegram_id)
        if user and first_purchase_bonus.bonus_window_open(user):
            tiers = await pricing_repo.get_first_purchase_bonus_tiers(pool)
            return PriceResponse(tiers=[_tier(tier) for tier in tiers])
        context = "onboarding_paywall"

    if context == "onboarding_paywall":
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payment_offered_at, has_purchased FROM users WHERE telegram_id = $1",
                telegram_id,
            )

        if row and row["has_purchased"]:
            tiers = await pricing_repo.get_menu_tiers(pool, bonus=False)
            return PriceResponse(tiers=[_tier(tier) for tier in tiers])

        tiers = await _get_first_purchase_offer_tiers(pool)
        return PriceResponse(tiers=[_tier(tier) for tier in tiers])

    tiers = await pricing_repo.get_menu_tiers(pool, bonus=context in MENU_CONTEXTS)
    return PriceResponse(tiers=[_tier(t) for t in tiers])


class FlowStartedRequest(BaseModel):
    telegram_id: int


@router.post("/flow-started", dependencies=[AuthDep])
async def flow_started(
    request: Request,
    body: FlowStartedRequest,
):
    """Called when user hits paywall from menu. Analytics only, no pushes."""
    await analytics.track(
        "payment_flow_started",
        user_id=str(body.telegram_id),
        properties={"source": "menu"},
    )
    return {"ok": True}


class OnboardingPaywallRequest(BaseModel):
    telegram_id: int
    # The bot sets it while onboarding v2 is on; it only marks the analytics event (before/after counts).
    onboarding_v2: bool = False


async def mark_onboarding_paywall_shown(
    request: Request,
    pool: asyncpg.Pool,
    telegram_id: int,
    onboarding_v2: bool = False,
) -> None:
    """Finalize either onboarding branch and arm the common payment offer."""
    await users_repo.mark_onboarding_completed(pool, telegram_id)
    await users_repo.complete_open_onboarding_trial(pool, telegram_id)

    await cancel_event(request, "onboarding_completed", telegram_id)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET payment_offered_at = NOW() WHERE telegram_id = $1 AND payment_offered_at IS NULL AND has_purchased = FALSE",
            telegram_id,
        )
        row = await conn.fetchrow(
            "SELECT id, first_name, gender FROM users WHERE telegram_id = $1",
            telegram_id,
        )

    if int(result.split()[-1]) > 0:
        event_name, extra = (
            await gift_push.paywall_event(
                pool, user_id=row["id"], platform="telegram", chat_id=telegram_id, gender=row["gender"],
            )
            if row else (gift_push.EVENT_CONTROL, {})
        )
        fire_event_bg(request, event_name, telegram_id, {
            "first_name": row["first_name"] or "" if row else "",
            **extra,
        })

    await analytics.track(
        "onboarding_paywall_shown",
        user_id=str(telegram_id),
        properties={"onboarding_v2": True} if onboarding_v2 else None,
    )


@router.post("/onboarding-paywall-shown", dependencies=[AuthDep])
async def onboarding_paywall_shown(
    request: Request,
    body: OnboardingPaywallRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Called after onboarding generation — shows paywall. Fires push notifications for nudging."""
    await mark_onboarding_paywall_shown(request, pool, body.telegram_id, onboarding_v2=body.onboarding_v2)
    return {"ok": True}


class PhotoFeedbackRequest(BaseModel):
    telegram_id: int
    generation_task_id: int
    feedback: str  # "like" or "dislike"


@router.post("/photo-feedback", dependencies=[AuthDep])
async def photo_feedback(
    body: PhotoFeedbackRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Save user feedback (like/dislike) on a generation task."""
    if body.feedback not in ("like", "dislike"):
        raise HTTPException(status_code=400, detail="feedback must be 'like' or 'dislike'")

    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    await users_repo.save_photo_feedback(
        pool, user_data["user"]["id"], body.generation_task_id, body.feedback,
    )

    await analytics.track(
        "photo_feedback",
        user_id=str(body.telegram_id),
        properties={
            "feedback": body.feedback,
            "generation_task_id": body.generation_task_id,
        },
    )
    return {"ok": True}


class PhotoRatingRequest(BaseModel):
    telegram_id: int
    generation_task_id: int
    score: int  # 1..5
    result_id: int | None = None
    context: str | None = None  # onboarding | photosession | trend


@router.post("/photo-rating", dependencies=[AuthDep])
async def photo_rating(
    body: PhotoRatingRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Save the 1-5 rating under a result (a second call changes it)."""
    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    return await photo_rating_service.rate_photo(
        pool, user_id=user_data["user"]["id"], platform="telegram",
        analytics_id=analytics_user_id("telegram", body.telegram_id),
        generation_task_id=body.generation_task_id, score=body.score,
        result_id=body.result_id, context=body.context,
    )


class PhotoFeedbackReasonRequest(BaseModel):
    telegram_id: int
    generation_task_id: int
    reason: str  # "face_mismatch", "unnatural", "style_dislike"


VALID_DISLIKE_REASONS = {"face_mismatch", "unnatural", "style_dislike"}


@router.post("/photo-feedback-reason", dependencies=[AuthDep])
async def photo_feedback_reason(
    body: PhotoFeedbackReasonRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Save dislike reason for photo feedback."""
    if body.reason not in VALID_DISLIKE_REASONS:
        raise HTTPException(status_code=400, detail=f"reason must be one of {VALID_DISLIKE_REASONS}")

    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    await users_repo.save_photo_feedback_reason(
        pool, user_data["user"]["id"], body.generation_task_id, body.reason,
    )

    await analytics.track(
        "photo_feedback_reason",
        user_id=str(body.telegram_id),
        properties={
            "reason": body.reason,
            "generation_task_id": body.generation_task_id,
        },
    )
    return {"ok": True}


class AddCreditsRequest(BaseModel):
    telegram_id: int
    generations: int
    comment: str = ""


@router.post("/add-credits", dependencies=[AuthDep])
async def add_credits(
    request: Request,
    body: AddCreditsRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Add generations after successful payment. Marks has_purchased."""
    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    user = user_data["user"]

    remaining = await photos_repo.add_generations(
        pool, user["id"], body.generations, body.comment,
    )

    is_first = await users_repo.mark_has_purchased(pool, body.telegram_id)

    await cancel_event(request, "onboarding_paywall_completed", body.telegram_id)
    await cancel_event(request, "first_payment_upsell_cancelled", body.telegram_id)

    if is_first:
        fire_event_bg(request, "first_payment_completed", body.telegram_id, {
            "first_name": user.get("first_name") or "",
        })

    await analytics.track(
        "credits_added",
        user_id=str(body.telegram_id),
        properties={
            "generations": body.generations,
            "new_balance": remaining,
            "comment": body.comment,
        },
    )

    return {"success": True, "generations_remaining": remaining}


# ── YooKassa endpoints ──────────────────────────────────────────────


class YookassaCreateRequest(BaseModel):
    telegram_id: int
    generations: int
    amount_rubles: int
    context: str | None = None
    offer_code: str | None = None


class PaymentCreateRequest(BaseModel):
    telegram_id: int
    generations: int
    amount_rubles: int
    context: str | None = None
    offer_code: str | None = None


class YookassaPaymentMessageRequest(BaseModel):
    telegram_id: int
    yookassa_id: str
    message_id: int


# --- T-BANK --- changed: yookassa_create
@router.post("/yookassa/create", dependencies=[AuthDep])
async def yookassa_create(
    body: YookassaCreateRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Create a card payment (YooKassa SBP, or T-Bank by the provider flag) and return the confirmation URL."""
    if not payment_gateway.yookassa_configured() and not tbank_acq.is_configured():
        raise HTTPException(status_code=503, detail="YooKassa not configured")

    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    user = user_data["user"]
    provider = payment_gateway.choose_provider(user["id"])
    if not payment_gateway.configured(provider):
        raise HTTPException(status_code=503, detail="YooKassa not configured")

    generations, amount_rubles = await _validate_payment_amount(pool, body)
    bonus_generations = await _menu_bonus_generations(pool, body.context, generations, amount_rubles)

    # Check if user already has pending payments
    existing_pending = await yookassa_repo.get_all_pending_by_telegram_id(pool, body.telegram_id)
    if existing_pending:
        logger.info(
            "YooKassa create: user=%s already has %d pending payment(s): %s",
            body.telegram_id, len(existing_pending),
            [p["yookassa_id"] for p in existing_pending],
        )

    result = await payment_gateway.create_payment(
        provider,
        amount_rubles=amount_rubles,
        description=f"{settings.BRAND_NAME} — пакет {generations} энергии",
        generations=generations,
        user_id=user["id"],
        telegram_id=body.telegram_id,
        # T-Bank: the flags that let the buttons of the bot ask for the SberPay / T-Pay link of this very payment.
        device_data=bot_pay.BOT_DEVICE_DATA if provider == payment_gateway.TBANK and settings.TBANK_BUTTONS else None,
    )

    await yookassa_repo.create_payment(
        pool=pool,
        user_id=user["id"],
        telegram_id=body.telegram_id,
        yookassa_id=result["yookassa_id"],
        amount_rubles=amount_rubles,
        generations=generations,
        confirmation_url=result["confirmation_url"],
        platform=_payment_platform(body.context),
        offer_slug=_offer_slug(body.context),
        bonus_generations=bonus_generations,
        provider=result.get("provider", "yookassa"),
        provider_order_id=result.get("order_id"),
    )

    logger.info(
        "YooKassa payment created: user=%s yookassa_id=%s amount=%s gens=%s",
        body.telegram_id, result["yookassa_id"], amount_rubles, generations,
    )

    await analytics.track(
        "yookassa_payment_created",
        user_id=str(body.telegram_id),
        properties={
            "amount_rubles": amount_rubles,
            "generations": generations,
            "bonus_generations": bonus_generations,
            "yookassa_id": result["yookassa_id"],
            "offer_slug": _offer_slug(body.context),
            "provider": result.get("provider", "yookassa"),
        },
    )

    return {
        "yookassa_id": result["yookassa_id"],
        "confirmation_url": result["confirmation_url"],
        "generations": generations,
        "amount_rubles": amount_rubles,
        "bonus_generations": bonus_generations,
        # T-Bank with the buttons on: ``pay_buttons`` (SberPay · SBP · T-Pay · card) and the page with the same choice as the
        # ``confirmation_url`` of a bot that does not know the list. The stored row keeps the bank's own form.
        **await bot_pay.fields_for_bot(
            provider=result.get("provider"), payment_id=result["yookassa_id"], user_id=user["id"], platform="tg",
        ),
    }
# --- /T-BANK ---


@router.post("/yookassa/payment-message", dependencies=[AuthDep])
async def yookassa_payment_message(
    body: YookassaPaymentMessageRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Save the payment card message ID for removal after a verified payment."""
    if body.message_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid Telegram message id")

    payment = await yookassa_repo.set_payment_message_id(
        pool, body.telegram_id, body.yookassa_id, body.message_id,
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


# --- T-BANK --- changed: yookassa_webhook
@router.post("/yookassa/webhook")
async def yookassa_webhook(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Handle YooKassa webhook notifications. No Bearer auth — public endpoint."""
    body = await request.json()
    event_type = body.get("event")
    payment_obj = body.get("object", {})
    yookassa_id = payment_obj.get("id")

    logger.info(
        "YooKassa webhook received: event=%s yookassa_id=%s body_keys=%s",
        event_type, yookassa_id, list(body.keys()),
    )

    if not yookassa_id:
        logger.warning("YooKassa webhook: missing yookassa_id in body")
        return {"ok": True}

    if event_type not in {"payment.succeeded", "payment.canceled"}:
        logger.info("YooKassa webhook: unhandled event=%s yookassa_id=%s", event_type, yookassa_id)
        return {"ok": True}

    local_payment = await yookassa_repo.get_by_yookassa_id(pool, yookassa_id)
    if local_payment is None:
        logger.warning("YooKassa webhook: unknown payment id=%s", yookassa_id)
        return {"ok": True}
    if payment_gateway.provider_of(local_payment) != payment_gateway.YOOKASSA:
        logger.warning("YooKassa webhook: id=%s belongs to another provider, ignored", yookassa_id)
        return {"ok": True}

    try:
        remote_payment = await yookassa_service.get_payment_status(yookassa_id)
    except Exception:
        logger.exception("YooKassa webhook: provider verification failed for id=%s", yookassa_id)
        raise HTTPException(status_code=503, detail="Payment verification unavailable")

    if event_type == "payment.succeeded":
        if remote_payment.get("status") != "succeeded" or not yookassa_service.matches_local_payment(
            remote_payment, local_payment,
        ):
            logger.warning("YooKassa webhook: rejected unverified success for id=%s", yookassa_id)
            return {"ok": True}
        await yookassa_repo.save_payment_method(pool, yookassa_id, remote_payment.get("payment_method"))
        paid = await _process_succeeded_payment(
            request, pool, yookassa_id, source="webhook",
        )
        if not paid:
            logger.warning(
                "YooKassa webhook: payment.succeeded but mark_succeeded returned None "
                "(already processed or not found): yookassa_id=%s",
                yookassa_id,
            )

    elif event_type == "payment.canceled":
        if remote_payment.get("status") != "canceled":
            logger.warning("YooKassa webhook: rejected unverified cancellation for id=%s", yookassa_id)
            return {"ok": True}
        payment = await yookassa_repo.mark_canceled(pool, yookassa_id)
        if payment:
            logger.info("YooKassa payment canceled (webhook): yookassa_id=%s", yookassa_id)
            if payment.get("telegram_id") is not None:
                await notify_payment_canceled(
                    request.app.state.notifications_client,
                    payment["telegram_id"],
                    settings.API_SECRET_TOKEN,
                    cleanup_message_id=payment.get("payment_message_id"),
                )
        else:
            logger.warning(
                "YooKassa webhook: payment.canceled but not found or already processed: yookassa_id=%s",
                yookassa_id,
            )

    else:
        logger.info("YooKassa webhook: unhandled event=%s yookassa_id=%s", event_type, yookassa_id)

    return {"ok": True}
# --- /T-BANK ---


# --- T-BANK --- new: tbank_notify
@router.post("/tbank/notify")
async def tbank_notify(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db),
):
    """T-Bank payment notifications. No Bearer auth — public endpoint, authenticated by the ``Token`` signature.

    T-Bank repeats a notification hourly until it gets HTTP 200 with the plain body ``OK``, and sends
    AUTHORIZED and CONFIRMED almost together, so every branch is idempotent (settlement is one conditional
    UPDATE). Anything that is not for us (other terminal, unknown payment) is answered ``OK`` to stop the
    repeats; a bad signature is a 403 and changes nothing.
    """
    if not tbank_acq.is_configured():
        raise HTTPException(status_code=503, detail="T-Bank not configured")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad JSON")
    if not isinstance(body, dict) or not tbank_acq.verify_notification(body):
        logger.warning("T-Bank notify: bad signature (payment_id=%s)", body.get("PaymentId") if isinstance(body, dict) else None)
        raise HTTPException(status_code=403, detail="Bad signature")
    ok = PlainTextResponse("OK")
    if str(body.get("TerminalKey")) != settings.TBANK_TERMINAL_KEY:
        logger.warning("T-Bank notify: foreign terminal, ignored")
        return ok

    payment_id = str(body.get("PaymentId") or "")
    order_id = body.get("OrderId")
    status = body.get("Status")
    local = await yookassa_repo.get_by_yookassa_id(pool, payment_id) if payment_id else None
    if (
        local is None
        or payment_gateway.provider_of(local) != payment_gateway.TBANK
        or local.get("provider_order_id") != order_id
    ):
        logger.warning("T-Bank notify: unknown payment id=%s order=%s status=%s", payment_id, order_id, status)
        return ok
    yookassa_id = local["yookassa_id"]
    logger.info("T-Bank notify: payment_id=%s status=%s local=%s", yookassa_id, status, local["status"])

    if status in tbank_acq.PAID_STATUSES:
        if body.get("Success") is False or str(body.get("ErrorCode") or "0") != "0":
            logger.warning("T-Bank notify: CONFIRMED with an error flag, ignored: id=%s", yookassa_id)
            return ok
        if str(body.get("Amount")) != str(tbank_acq.kopecks(local["amount_rubles"])):
            logger.error(
                "T-Bank notify: amount mismatch id=%s notified=%s expected=%s",
                yookassa_id, body.get("Amount"), tbank_acq.kopecks(local["amount_rubles"]),
            )
            return ok
        if local["status"] == "canceled" and await yookassa_repo.reopen_canceled(pool, yookassa_id):
            logger.warning("T-Bank notify: payment %s was closed as failed but is paid — reopened", yookassa_id)
        await yookassa_repo.save_payment_method(pool, yookassa_id, tbank_acq.notification_card(body))
        paid = await _process_succeeded_payment(request, pool, yookassa_id, source="webhook")
        if not paid:
            logger.info("T-Bank notify: CONFIRMED but already processed or not pending: id=%s", yookassa_id)
        if local.get("payment_method_type") is None:
            await _save_tbank_method(pool, yookassa_id)

    elif status in tbank_acq.FAILED_STATUSES:
        payment = await yookassa_repo.mark_canceled(pool, yookassa_id)
        if payment:
            logger.info("T-Bank payment canceled (webhook): id=%s status=%s", yookassa_id, status)
            if payment.get("telegram_id") is not None:
                await notify_payment_canceled(
                    request.app.state.notifications_client,
                    payment["telegram_id"],
                    settings.API_SECRET_TOKEN,
                    cleanup_message_id=payment.get("payment_message_id"),
                )

    elif status in tbank_acq.REFUND_STATUSES:
        # Refunds we make (inbox → Cancel) are recorded from the Cancel answer. A refund made in the T-Bank
        # cabinet is recorded here when it is a full one; the amount of a partial refund is not in the
        # notification (open question, see the report) — it is logged for a manual entry.
        if status == "REFUNDED":
            await yookassa_repo.record_refund(pool, yookassa_id, local["amount_rubles"])
        else:
            logger.warning(
                "T-Bank notify: PARTIAL_REFUNDED for %s, refunded amount unknown — check the cabinet "
                "and set yookassa_payments.refunded_rubles", yookassa_id,
            )

    return ok
# --- /T-BANK ---


TBANK_METHOD_TIMEOUT = 8.0  # --- T-BANK ---


# --- T-BANK --- new: _save_tbank_method
async def _save_tbank_method(pool: asyncpg.Pool, yookassa_id: str) -> None:
    """Write how a T-Bank payment was paid (sberbank / sbp / tinkoff_bank / bank_card …).

    The notification does not say (its ``Pan`` is a card mask or a phone), only ``GetState`` has ``Params[Source]``.
    It runs after the payment is credited and is bounded, so a slow or failing bank can neither hold back the credit nor
    the ``OK`` answer; the type then stays empty for the next repeat of the notification and ``backfill_tbank_method.py``.
    """
    try:
        remote = await asyncio.wait_for(tbank_acq.get_payment_status(yookassa_id), timeout=TBANK_METHOD_TIMEOUT)
    except Exception:
        logger.warning("T-Bank notify: could not read the payment method of %s", yookassa_id, exc_info=True)
        return
    await yookassa_repo.save_payment_method(pool, yookassa_id, remote.get("payment_method"))
# --- /T-BANK ---


async def _process_succeeded_payment(
    request: Request,
    pool: asyncpg.Pool,
    yookassa_id: str,
    source: str,
) -> dict | None:
    """Mark payment as succeeded, credit generations, fire notifications.

    Returns the updated payment row or None if already processed.
    """
    paid = await yookassa_repo.settle_succeeded_payment(pool, yookassa_id)
    if not paid:
        logger.info(
            "YooKassa payment already processed (%s): yookassa_id=%s",
            source, yookassa_id,
        )
        return None

    return await _finalize_credited_payment(
        request, pool, paid, method="yookassa", source=source,
    )


# --- T-BANK --- changed: _finalize_credited_payment
async def _finalize_credited_payment(
    request: Request,
    pool: asyncpg.Pool,
    paid: dict,
    *,
    method: str,
    source: str,
    send_chat_notification: bool = True,
) -> dict:
    """Fire every side effect that follows a durably credited payment.

    Shared by YooKassa (``method="yookassa"``) and Telegram Stars
    (``method="telegram_stars"``). ``paid`` is the settlement row from the
    matching repo; both expose the same keys. Stars settlement is instant and
    the bot shows its own success screen, so it passes
    ``send_chat_notification=False`` to suppress the duplicate push.
    """
    is_stars = method == "telegram_stars"
    is_lava = method == "lava"
    is_tbank = paid.get("provider") == "tbank"
    label = "Stars" if is_stars else "lava.top" if is_lava else "T-Bank" if is_tbank else "YooKassa"
    ref = paid.get("yookassa_id") or paid.get("payload") or paid.get("contract_id")
    remaining = paid["new_balance"]
    credited_generations = paid.get("credited_generations", paid["generations"])
    bonus_generations = paid.get("bonus_generations", 0)
    logger.info(
        "%s payment succeeded (%s): user=%s tg=%s platform=%s ref=%s gens=%s amount=%s balance=%s",
        label,
        source, paid["user_id"], paid.get("telegram_id"), paid.get("platform"), ref,
        paid["generations"],
        f"{paid['stars']}XTR" if is_stars
        else f"{paid['amount']}{paid['currency']}" if is_lava
        else f"{paid['amount_rubles']}RUB",
        remaining,
    )

    is_first = paid["is_first_purchase"]
    analytics_uid = await _analytics_user_id(pool, paid)
    await notify_referrer(notifications_client_for(request), pool, paid.get("referrer_reward"))

    # Telegram-only side effects: the notifications service and its scheduled
    # funnel events are Telegram-native.
    if paid.get("telegram_id") is not None:
        await cancel_event(request, "onboarding_paywall_completed", paid["telegram_id"])
        await cancel_event(request, "first_payment_upsell_cancelled", paid["telegram_id"])
        if send_chat_notification:
            await notify_payment_succeeded(
                request.app.state.notifications_client,
                paid["telegram_id"],
                credited_generations,
                remaining,
                settings.BOT_USERNAME,
                settings.API_SECRET_TOKEN,
                cleanup_message_id=paid.get("payment_message_id"),
            )

        if is_first:
            async with pool.acquire() as conn:
                user_row = await conn.fetchrow(
                    "SELECT first_name FROM users WHERE id = $1",
                    paid["user_id"],
                )
            fire_event_bg(request, "first_payment_completed", paid["telegram_id"], {
                "first_name": (user_row["first_name"] if user_row else "") or "",
            })
            logger.info(
                "%s first purchase (%s): user=%s ref=%s",
                label, source, paid["user_id"], ref,
            )
    elif str(paid.get("platform") or "").startswith("max"):
        # "poll" means the user is tapping "Проверить оплату" in the bot right
        # now: the flow itself is about to show its own success screen (with a
        # menu), so this raw backend push would just be an ugly duplicate. Only
        # the webhook path — settlement the user never asked the bot about —
        # needs it, since it's the only notice they'll get in that case.
        if source != "poll":
            await max_bot.notify_payment_succeeded(
                pool,
                user_id=paid["user_id"],
                generations=credited_generations,
                new_balance=remaining,
                amount_rubles=paid["amount_rubles"],
                bonus_generations=bonus_generations,
            )
        max_client = notifications_client_for(request, "max")
        if max_client is not None:
            recipient = await pool.fetchval(
                "SELECT external_id FROM user_identities WHERE user_id = $1 AND platform = 'max'",
                paid["user_id"],
            )
            try:
                recipient = int(recipient) if recipient is not None else None
            except (TypeError, ValueError):
                recipient = None
            if recipient is not None:
                await max_client.cancel_event("onboarding_paywall_completed", recipient)
                await max_client.cancel_event("first_payment_upsell_cancelled", recipient)
                if is_first:
                    async with pool.acquire() as conn:
                        first_name = await conn.fetchval(
                            "SELECT first_name FROM users WHERE id = $1", paid["user_id"],
                        )
                    await max_client.fire_event(
                        "first_payment_completed", recipient, {"first_name": first_name or ""},
                    )

    if paid.get("test"):
        # Test-terminal payment (status test_owner): no purchase events, nothing for the funnel or the ad systems.
        logger.info("T-Bank TEST payment credited, purchase analytics skipped: ref=%s", ref)
        return paid
    await analytics.track(
        "stars_payment_succeeded" if is_stars
        else "lava_payment_succeeded" if is_lava
        else "yookassa_payment_succeeded",
        user_id=analytics_uid,
        properties={
            ("payload" if is_stars else "contract_id" if is_lava else "yookassa_id"): ref,
            ("stars" if is_stars else "amount_rubles"): (
                paid["stars"] if is_stars else paid["amount_rubles"]
            ),
            "amount_rubles": paid["amount_rubles"],
            "generations": paid["generations"],
            "bonus_generations": bonus_generations,
            "credited_generations": credited_generations,
            "new_balance": remaining,
            "source": source,
            "offer_slug": paid.get("offer_slug"),
            "provider": "tbank" if is_tbank else "yookassa",
        },
    )
    await analytics.track(
        "payment_completed",
        user_id=analytics_uid,
        properties={
            "amount": (
                paid["stars"] if is_stars
                else float(paid["amount"] or 0) if is_lava
                else paid["amount_rubles"]
            ),
            "currency": "XTR" if is_stars else paid["currency"] if is_lava else "RUB",
            "method": "tbank" if is_tbank else method,
            "amount_rubles": paid["amount_rubles"],
            "generations": paid["generations"],
            "bonus_generations": bonus_generations,
            "credited_generations": credited_generations,
            "new_balance": remaining,
            "platform": paid.get("platform") or "telegram",
            "offer_slug": paid.get("offer_slug"),
        },
    )
    return paid
# --- /T-BANK ---


async def _analytics_user_id(pool: asyncpg.Pool, paid: dict) -> str:
    """Analytics namespace: Telegram keeps its raw id for history continuity;
    other platforms use ``<platform>:<external_id>`` (PLAN.md Track E)."""
    if paid.get("telegram_id") is not None:
        return str(paid["telegram_id"])
    platform = paid.get("platform") or "telegram"
    external_id = await pool.fetchval(
        "SELECT external_id FROM user_identities WHERE user_id = $1 AND platform = $2",
        paid["user_id"], platform,
    )
    return analytics_user_id(platform, external_id or paid["user_id"])


# --- T-BANK --- changed: yookassa_status
@router.get("/yookassa/status", dependencies=[AuthDep])
async def yookassa_status(
    request: Request,
    telegram_id: int,
    yookassa_id: str | None = None,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Check YooKassa payment status.

    If pending — polls YooKassa API for ALL pending payments of this user
    to handle the case when user paid an older payment but is polling a newer one.
    """
    # Get ALL pending payments for this user
    all_pending = await yookassa_repo.get_all_pending_by_telegram_id(pool, telegram_id)
    logger.info(
        "YooKassa status poll: telegram_id=%s requested_yookassa_id=%s pending_count=%d pending_ids=%s",
        telegram_id, yookassa_id, len(all_pending),
        [p["yookassa_id"] for p in all_pending],
    )

    # Poll ALL pending payments against YooKassa API and process them
    succeeded_payment = None
    for pending in all_pending:
        try:
            yk_status = await payment_gateway.get_payment_status(pending)
            remote_status = yk_status.get("status")
            logger.info(
                "YooKassa API status: yookassa_id=%s local=pending remote=%s user=%s",
                pending["yookassa_id"], remote_status, telegram_id,
            )

            if remote_status == "succeeded":
                if not payment_gateway.matches_local_payment(yk_status, pending):
                    logger.error(
                        "YooKassa payment details mismatch: yookassa_id=%s user=%s",
                        pending["yookassa_id"], telegram_id,
                    )
                    continue
                await yookassa_repo.save_payment_method(pool, pending["yookassa_id"], yk_status.get("payment_method"))
                paid = await _process_succeeded_payment(
                    request, pool, pending["yookassa_id"], source="poll",
                )
                if paid and succeeded_payment is None:
                    succeeded_payment = paid

            elif remote_status == "canceled":
                canceled = await yookassa_repo.mark_canceled(pool, pending["yookassa_id"])
                if canceled:
                    await notify_payment_canceled(
                        request.app.state.notifications_client,
                        canceled["telegram_id"],
                        settings.API_SECRET_TOKEN,
                        cleanup_message_id=canceled.get("payment_message_id"),
                    )
                logger.info(
                    "YooKassa payment canceled (poll): yookassa_id=%s user=%s",
                    pending["yookassa_id"], telegram_id,
                )

        except Exception as e:
            logger.error(
                "Failed to poll YooKassa status: yookassa_id=%s user=%s error=%s",
                pending["yookassa_id"], telegram_id, e,
            )

    # Determine which payment to return status for
    if succeeded_payment:
        # A payment just got processed — return it
        payment = await yookassa_repo.get_by_yookassa_id(pool, succeeded_payment["yookassa_id"])
    elif yookassa_id:
        # Return the specifically requested payment
        payment = await yookassa_repo.get_by_yookassa_id(pool, yookassa_id)
        if payment and payment["telegram_id"] != telegram_id:
            logger.warning(
                "YooKassa status poll: payment ownership mismatch for user=%s",
                telegram_id,
            )
            payment = None
    else:
        # Return the latest payment
        payment = await yookassa_repo.get_pending_by_telegram_id(pool, telegram_id)

    if not payment:
        logger.info("YooKassa status poll: no payment found for user=%s", telegram_id)
        return {"found": False}

    return {
        "found": True,
        "yookassa_id": payment["yookassa_id"],
        "status": payment["status"],
        "generations": payment["generations"],
        "amount_rubles": payment["amount_rubles"],
    }
# --- /T-BANK ---


# ── Telegram Stars endpoints (fallback checkout for non-RU users) ──
#
# Stars settle in one shot via the bot's ``successful_payment`` update, so there
# is no webhook and no status-poll loop: the bot calls /create, then /precheckout
# inside answerPreCheckoutQuery, then /confirm exactly once when the payment lands.


class StarsCreateRequest(BaseModel):
    telegram_id: int
    generations: int
    amount_rubles: int
    context: str | None = None
    offer_code: str | None = None


class StarsPreCheckoutRequest(BaseModel):
    telegram_id: int
    payload: str


class StarsConfirmRequest(BaseModel):
    telegram_id: int
    payload: str
    charge_id: str
    total_amount: int
    message_id: int | None = None


class StarsRefundRequest(BaseModel):
    telegram_id: int
    charge_id: str


@router.post("/stars/create", dependencies=[AuthDep])
async def stars_create(
    body: StarsCreateRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Register a pending Telegram Stars payment and return the invoice numbers."""
    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    user = user_data["user"]

    generations, amount_rubles = await _validate_payment_amount(pool, body)
    menu_bonus = await _menu_bonus_generations(pool, body.context, generations, amount_rubles)

    markup = await pricing_repo.get_stars_markup_percent(pool)
    stars = pricing_repo.rubles_to_stars(amount_rubles, markup)
    if stars <= 0:
        raise HTTPException(status_code=400, detail="Stars price unavailable for this tier")

    platform = _payment_platform(body.context)
    payload = uuid4().hex

    await stars_repo.create_payment(
        pool,
        user_id=user["id"],
        telegram_id=body.telegram_id,
        payload=payload,
        stars=stars,
        generations=generations,
        amount_rubles=amount_rubles,
        platform=platform,
        context=body.context or "menu",
        offer_slug=_offer_slug(body.context),
        bonus_generations=menu_bonus,
    )

    bonus = (
        first_purchase_bonus.bonus_generations(generations) if platform == "telegram_bonus" else menu_bonus
    )
    if bonus:
        description = f"{generations} энергии + {bonus} бонусом сразу на баланс. Без подписки."
    else:
        description = f"{generations} энергии на баланс. Без подписки."

    logger.info(
        "Stars payment created: tg=%s payload=%s stars=%s gens=%s rub=%s platform=%s",
        body.telegram_id, payload, stars, generations, amount_rubles, platform,
    )
    await analytics.track(
        "stars_payment_created",
        user_id=str(body.telegram_id),
        properties={
            "payload": payload,
            "stars": stars,
            "amount_rubles": amount_rubles,
            "generations": generations,
            "context": body.context or "menu",
            "offer_slug": _offer_slug(body.context),
        },
    )

    return {
        "payload": payload,
        "stars": stars,
        "generations": generations,
        "amount_rubles": amount_rubles,
        "bonus_generations": menu_bonus,
        "title": f"{settings.BRAND_NAME} — {generations} энергии",
        "description": description,
    }


@router.post("/stars/precheckout", dependencies=[AuthDep])
async def stars_precheckout(
    body: StarsPreCheckoutRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Last-chance validation the bot runs inside answerPreCheckoutQuery (10s budget)."""
    row = await stars_repo.get_by_payload(pool, body.payload)
    if row is None or row["telegram_id"] != body.telegram_id:
        return {"ok": False, "error": "Платёж не найден."}
    if row["status"] != "pending":
        return {"ok": False, "error": "Этот платёж уже обработан."}
    return {"ok": True, "error": None}


@router.post("/stars/confirm", dependencies=[AuthDep])
async def stars_confirm(
    request: Request,
    body: StarsConfirmRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Credit generations for a completed Telegram Stars payment. Idempotent."""
    row = await stars_repo.get_by_payload(pool, body.payload)
    if row is None or row["telegram_id"] != body.telegram_id:
        logger.warning("Stars confirm: unknown payload=%s tg=%s", body.payload, body.telegram_id)
        return {"found": False}

    if row["status"] == "succeeded":
        # The bot retried after a transient failure — the credit already landed.
        return {
            "found": True,
            "status": "succeeded",
            "already": True,
            "generations": row["generations"],
        }

    if body.total_amount != row["stars"]:
        logger.error(
            "Stars confirm: amount mismatch payload=%s charged=%s expected=%s",
            body.payload, body.total_amount, row["stars"],
        )
        raise HTTPException(status_code=400, detail="Payment amount mismatch")

    if body.message_id:
        await stars_repo.set_payment_message_id(
            pool, body.telegram_id, body.payload, body.message_id,
        )

    paid = await stars_repo.settle_succeeded_payment(
        pool, payload=body.payload, charge_id=body.charge_id,
    )
    if paid is None:
        # Lost a race with a concurrent confirm — read back the settled state.
        row = await stars_repo.get_by_payload(pool, body.payload)
        return {
            "found": True,
            "status": row["status"] if row else "unknown",
            "already": True,
            "generations": row["generations"] if row else 0,
        }

    await _finalize_credited_payment(
        request, pool, paid,
        method="telegram_stars",
        source="successful_payment",
        send_chat_notification=False,
    )

    return {
        "found": True,
        "status": "succeeded",
        "generations": paid["credited_generations"],
        "bonus_generations": paid["bonus_generations"],
        "new_balance": paid["new_balance"],
    }


@router.post("/stars/refund", dependencies=[AuthDep])
async def stars_refund(
    body: StarsRefundRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Reverse a settled Stars payment after the bot has issued the Telegram refund."""
    row = await stars_repo.get_by_charge_id(pool, body.charge_id)
    if row is None or row["telegram_id"] != body.telegram_id:
        return {"ok": False, "error": "Payment not found"}
    if row["status"] != "succeeded":
        return {"ok": False, "error": f"Payment is {row['status']}"}

    bonus = (
        first_purchase_bonus.bonus_generations(row["generations"])
        if await stars_repo.bonus_was_credited(pool, row) else 0
    )
    result = await stars_repo.mark_refunded(
        pool, charge_id=body.charge_id,
        debit_generations=row["generations"] + bonus + int(row.get("bonus_generations") or 0),
    )
    logger.info("Stars payment refunded: tg=%s charge=%s", body.telegram_id, body.charge_id)
    await analytics.track(
        "stars_payment_refunded",
        user_id=str(body.telegram_id),
        properties={"charge_id": body.charge_id, "stars": row["stars"]},
    )
    return {"ok": result is not None, "new_balance": result["new_balance"] if result else None}


# ── Robokassa endpoints (international cards) ───────────────────────


class RobokassaPaymentMessageRequest(BaseModel):
    telegram_id: int
    inv_id: int
    message_id: int


@router.post("/robokassa/create", dependencies=[AuthDep])
async def robokassa_create(
    body: PaymentCreateRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Create a Robokassa payment URL for a card issued outside Russia."""
    # The foreign-card checkout is retired: it converted at ~1% and funnelled
    # users into a dead end. New invoices are refused; /robokassa/result and the
    # status check stay live so already-created invoices can still settle.
    raise HTTPException(status_code=503, detail="Foreign-card payments are unavailable")
    if not robokassa_service.is_configured():
        raise HTTPException(status_code=503, detail="Robokassa not configured")
    if settings.ROBOKASSA_TEST_MODE and body.telegram_id not in settings.robokassa_test_user_ids:
        raise HTTPException(status_code=403, detail="Robokassa test mode is private")

    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    generations, offer_rubles = await _validate_payment_amount(pool, body)
    # This endpoint is only used by the international-card provider. YooKassa
    # keeps the validated offer price unchanged in its separate endpoint.
    if offer_rubles < settings.ROBOKASSA_MIN_RUBLES:
        generations = settings.ROBOKASSA_MIN_GENERATIONS
        amount_rubles = settings.ROBOKASSA_MIN_RUBLES
    else:
        amount_rubles = offer_rubles
    payment = await robokassa_repo.create_payment(
        pool,
        user_id=user_data["user"]["id"],
        telegram_id=body.telegram_id,
        amount_rubles=amount_rubles,
        generations=generations,
    )
    inv_id = payment["inv_id"]
    payment_url = robokassa_service.build_payment_url(
        amount_rubles=amount_rubles,
        inv_id=inv_id,
        # Robokassa expects a simple product description. Keep it free from
        # punctuation that can be rejected by card processors.
        description=f"{settings.BRAND_NAME} {generations} generations",
    )
    await robokassa_repo.set_payment_url(pool, inv_id, payment_url)

    logger.info(
        "Robokassa payment created: user=%s inv_id=%s amount=%s gens=%s",
        body.telegram_id, inv_id, amount_rubles, generations,
    )
    await analytics.track(
        "robokassa_payment_created",
        user_id=str(body.telegram_id),
        properties={
            "amount_rubles": amount_rubles,
            "generations": generations,
            "inv_id": inv_id,
        },
    )
    return {
        "inv_id": inv_id,
        "confirmation_url": payment_url,
        "generations": generations,
        "amount_rubles": amount_rubles,
    }


@router.post("/robokassa/payment-message", dependencies=[AuthDep])
async def robokassa_payment_message(
    body: RobokassaPaymentMessageRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Save the bot card message for removal after verified payment."""
    if body.inv_id <= 0 or body.message_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid payment message")
    payment = await robokassa_repo.set_payment_message_id(
        pool, body.telegram_id, body.inv_id, body.message_id,
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


async def _robokassa_request_data(request: Request) -> dict[str, str]:
    """Accept the ResultURL callback in either configured GET or POST form."""
    data = {key: value for key, value in request.query_params.items()}
    if request.method == "POST":
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid Robokassa callback") from exc
        data.update({key: str(value) for key, value in form.items()})
    return data


async def _process_robokassa_success(
    request: Request,
    pool: asyncpg.Pool,
    inv_id: int,
) -> dict | None:
    paid = await robokassa_repo.settle_succeeded_payment(pool, inv_id)
    if not paid:
        logger.info("Robokassa invoice already processed: inv_id=%s", inv_id)
        return None

    remaining = paid["new_balance"]
    await notify_referrer(notifications_client_for(request), pool, paid.get("referrer_reward"))
    await cancel_event(request, "onboarding_paywall_completed", paid["telegram_id"])
    await cancel_event(request, "first_payment_upsell_cancelled", paid["telegram_id"])
    await notify_payment_succeeded(
        request.app.state.notifications_client,
        paid["telegram_id"],
        paid["credited_generations"],
        remaining,
        settings.BOT_USERNAME,
        settings.API_SECRET_TOKEN,
        cleanup_message_id=paid.get("payment_message_id"),
    )

    if paid["is_first_purchase"]:
        async with pool.acquire() as conn:
            user_row = await conn.fetchrow(
                "SELECT first_name FROM users WHERE telegram_id = $1",
                paid["telegram_id"],
            )
        fire_event_bg(request, "first_payment_completed", paid["telegram_id"], {
            "first_name": (user_row["first_name"] if user_row else "") or "",
        })

    await analytics.track(
        "robokassa_payment_succeeded",
        user_id=str(paid["telegram_id"]),
        properties={
            "inv_id": inv_id,
            "amount_rubles": paid["amount_rubles"],
            "generations": paid["generations"],
            "new_balance": remaining,
        },
    )
    await analytics.track(
        "payment_completed",
        user_id=str(paid["telegram_id"]),
        properties={
            "amount": paid["amount_rubles"],
            "currency": "RUB",
            "method": "robokassa_international_card",
            "generations": paid["generations"],
            "new_balance": remaining,
        },
    )
    logger.info(
        "Robokassa payment succeeded: user=%s inv_id=%s gens=%s amount=%s balance=%s",
        paid["telegram_id"], inv_id, paid["generations"], paid["amount_rubles"], remaining,
    )
    return paid


@router.api_route("/robokassa/result", methods=["GET", "POST"])
async def robokassa_result(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Verified ResultURL callback from Robokassa. Its response must be exactly OK<InvId>."""
    data = await _robokassa_request_data(request)
    out_sum = data.get("OutSum", "")
    inv_id_raw = data.get("InvId") or data.get("InvID") or data.get("InvoiceID") or ""
    signature = data.get("SignatureValue", "")
    try:
        inv_id = int(inv_id_raw)
    except (TypeError, ValueError):
        return PlainTextResponse("Invalid InvId", status_code=400)

    if inv_id <= 0 or not robokassa_service.verify_result_signature(out_sum, inv_id, signature):
        logger.warning("Robokassa ResultURL rejected: inv_id=%s", inv_id_raw)
        return PlainTextResponse("Invalid signature", status_code=400)

    payment = await robokassa_repo.get_by_inv_id(pool, inv_id)
    if payment is None:
        logger.warning("Robokassa ResultURL for unknown invoice: inv_id=%s", inv_id)
        return PlainTextResponse("Unknown invoice", status_code=404)
    if not robokassa_service.matches_amount(out_sum, payment["amount_rubles"]):
        logger.warning("Robokassa ResultURL amount mismatch: inv_id=%s out_sum=%s", inv_id, out_sum)
        return PlainTextResponse("Invalid amount", status_code=400)

    await _process_robokassa_success(request, pool, inv_id)
    return PlainTextResponse(f"OK{inv_id}")


@router.get("/robokassa/status", dependencies=[AuthDep])
async def robokassa_status(
    telegram_id: int,
    inv_id: int,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Return only a locally verified settlement status; ResultURL is authoritative."""
    payment = await robokassa_repo.get_by_inv_id(pool, inv_id)
    if payment is None or payment["telegram_id"] != telegram_id:
        return {"found": False}
    return {
        "found": True,
        "inv_id": payment["inv_id"],
        "status": payment["status"],
        "generations": payment["generations"],
        "amount_rubles": payment["amount_rubles"],
    }


# --- lava.top (USD/EUR cards issued outside Russia) -------------------------


class LavaCreateRequest(BaseModel):
    telegram_id: int
    generations: int
    amount_rubles: int
    context: str | None = None
    offer_code: str | None = None
    # Buyer e-mail for the lava.top receipt; omitted → the one given last time.
    email: str | None = None


class LavaPaymentMessageRequest(BaseModel):
    telegram_id: int
    contract_id: str
    message_id: int


@router.post("/lava/create", dependencies=[AuthDep])
async def lava_create(
    body: LavaCreateRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Create a lava.top contract for a package and return its payment URL.

    The package is validated against the live ruble tiers exactly like the
    other checkouts, then priced in USD/EUR at the configured rate; the single
    lava.top offer is published with «Цена по запросу API».
    """
    if not lava_service.is_configured():
        raise HTTPException(status_code=503, detail="lava.top not configured")
    if discount_ladder.offer_from_context(body.context) is not None:
        raise HTTPException(status_code=400, detail="Discount offers are paid by card (YooKassa) or Stars")

    user_data = await users_repo.get_user_by_telegram_id(pool, body.telegram_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")
    user = user_data["user"]

    generations, amount_rubles = await _validate_payment_amount(pool, body)
    bonus_generations = await _menu_bonus_generations(pool, body.context, generations, amount_rubles)
    offer_id = settings.LAVA_OFFER_ID
    amount = lava_service.price_for(amount_rubles)

    platform = _payment_platform(body.context)
    if body.email is not None:
        email = lava_service.normalize_email(body.email)
        if email is None:
            raise HTTPException(status_code=400, detail="invalid_email")
    else:
        email = await lava_repo.last_buyer_email(pool, user["id"])
        if email is None:
            # The bot asks the buyer for an e-mail and retries with it.
            raise HTTPException(status_code=409, detail="email_required")
    try:
        invoice = await lava_service.create_invoice(
            offer_id=offer_id,
            email=email,
            amount=amount,
            currency=settings.LAVA_CURRENCY,
            return_url=f"https://t.me/{settings.BOT_USERNAME}",
        )
    except Exception:
        logger.exception("lava.top create failed: tg=%s gens=%s", body.telegram_id, generations)
        raise HTTPException(status_code=502, detail="lava.top unavailable")

    await lava_repo.create_payment(
        pool,
        user_id=user["id"],
        telegram_id=body.telegram_id,
        contract_id=invoice["contract_id"],
        offer_id=offer_id,
        buyer_email=email,
        amount=invoice["amount"],
        currency=invoice["currency"],
        generations=generations,
        amount_rubles=amount_rubles,
        payment_url=invoice["payment_url"],
        platform=platform,
        context=body.context or "menu",
        bonus_generations=bonus_generations,
    )
    logger.info(
        "lava.top payment created: tg=%s contract=%s gens=%s amount=%s %s platform=%s",
        body.telegram_id, invoice["contract_id"], generations,
        invoice["amount"], invoice["currency"], platform,
    )
    await analytics.track(
        "lava_payment_created",
        user_id=str(body.telegram_id),
        properties={
            "contract_id": invoice["contract_id"],
            "amount": float(invoice["amount"] or 0),
            "currency": invoice["currency"],
            "amount_rubles": amount_rubles,
            "generations": generations,
            "context": body.context or "menu",
        },
    )
    return {
        "contract_id": invoice["contract_id"],
        "payment_url": invoice["payment_url"],
        "amount": float(invoice["amount"]) if invoice["amount"] is not None else None,
        "currency": invoice["currency"],
        "generations": generations,
        "amount_rubles": amount_rubles,
        "bonus_generations": bonus_generations,
    }


@router.post("/lava/payment-message", dependencies=[AuthDep])
async def lava_payment_message(
    body: LavaPaymentMessageRequest,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Save the bot checkout message for removal after verified payment."""
    if body.message_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid payment message")
    payment = await lava_repo.set_payment_message_id(
        pool, body.telegram_id, body.contract_id, body.message_id,
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


async def _settle_lava_contract(
    request: Request, pool: asyncpg.Pool, contract_id: str, *, source: str,
) -> dict | None:
    """Confirm the contract with lava.top, then credit it exactly once."""
    remote = await lava_service.get_invoice(contract_id)
    if not lava_service.is_completed(remote):
        logger.warning(
            "lava.top %s: contract %s not completed remotely (status=%s)",
            source, contract_id, remote.get("status"),
        )
        return None
    receipt = remote.get("receipt") or {}
    paid = await lava_repo.settle_succeeded_payment(
        pool, contract_id=contract_id, amount=lava_service.to_decimal(receipt.get("amount")),
    )
    if not paid:
        logger.info("lava.top payment already processed (%s): contract=%s", source, contract_id)
        return None
    return await _finalize_credited_payment(request, pool, paid, method="lava", source=source)


@router.post("/lava/webhook")
async def lava_webhook(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db),
):
    """lava.top webhook (API-key auth). Public endpoint — no Bearer token."""
    if not lava_service.verify_webhook(request.headers.get("X-Api-Key")):
        logger.warning("lava.top webhook: bad X-Api-Key from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="Invalid webhook key")

    body = await request.json()
    # Payment events use camelCase (eventType/contractId); refund and
    # chargeback events use snake_case with a nested ``data`` object.
    event_type = body.get("eventType") or body.get("event_type")
    logger.info("lava.top webhook: event=%s keys=%s", event_type, list(body.keys()))

    if event_type == "payment.success":
        contract_id = body.get("contractId")
        if not contract_id or await lava_repo.get_by_contract_id(pool, contract_id) is None:
            logger.warning("lava.top webhook: unknown contract %s", contract_id)
            return {"ok": True}
        try:
            await _settle_lava_contract(request, pool, contract_id, source="webhook")
        except lava_service.LavaError:
            logger.exception("lava.top webhook: verification failed for %s", contract_id)
            raise HTTPException(status_code=503, detail="Payment verification unavailable")
        return {"ok": True}

    if event_type == "payment.failed":
        contract_id = body.get("contractId")
        if contract_id:
            failed = await lava_repo.mark_failed(pool, contract_id, body.get("errorMessage"))
            if failed:
                logger.info("lava.top payment failed: contract=%s reason=%s", contract_id, body.get("errorMessage"))
        return {"ok": True}

    if event_type in {"refund.success", "chargeback.initiated"}:
        data = body.get("data") or {}
        email = data.get("customer_email") or (data.get("buyer") or {}).get("email")
        amount = lava_service.to_decimal(data.get("amount"))
        payment = await lava_repo.find_refundable(pool, buyer_email=email or "", amount=amount)
        if payment is None:
            logger.warning("lava.top %s: no settled payment for email=%s amount=%s", event_type, email, amount)
            return {"ok": True}
        status = "refunded" if event_type == "refund.success" else "chargeback"
        reversed_payment = await lava_repo.mark_refunded(pool, payment_id=payment["id"], status=status)
        if reversed_payment:
            logger.warning(
                "lava.top %s: user=%s contract=%s debited=%s balance=%s",
                event_type, reversed_payment["user_id"], reversed_payment["contract_id"],
                reversed_payment["debited_generations"], reversed_payment["new_balance"],
            )
            await analytics.track(
                "lava_payment_refunded",
                user_id=str(reversed_payment.get("telegram_id") or reversed_payment["user_id"]),
                properties={
                    "contract_id": reversed_payment["contract_id"],
                    "kind": status,
                    "debited_generations": reversed_payment["debited_generations"],
                },
            )
        return {"ok": True}

    logger.info("lava.top webhook: unhandled event=%s", event_type)
    return {"ok": True}


@router.get("/lava/status", dependencies=[AuthDep])
async def lava_status(
    request: Request,
    telegram_id: int,
    contract_id: str,
    pool: asyncpg.Pool = Depends(get_db),
):
    """«Проверить оплату»: settle from lava.top if the webhook is late."""
    payment = await lava_repo.get_by_contract_id(pool, contract_id)
    if payment is None or payment["telegram_id"] != telegram_id:
        return {"found": False}
    if payment["status"] in ("pending", "failed") and lava_service.is_configured():
        try:
            await _settle_lava_contract(request, pool, contract_id, source="poll")
        except lava_service.LavaError:
            logger.warning("lava.top status poll failed for %s", contract_id)
        payment = await lava_repo.get_by_contract_id(pool, contract_id)
    return {
        "found": True,
        "contract_id": payment["contract_id"],
        "status": payment["status"],
        "generations": payment["generations"],
        "credited_generations": payment["credited_generations"],
        "amount": float(payment["amount"]) if payment["amount"] is not None else None,
        "currency": payment["currency"],
    }
