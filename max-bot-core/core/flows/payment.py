"""Shared external-payment flow for Telegram and MAX."""
from __future__ import annotations

import uuid
from typing import Protocol

from core import onboarding_v2
from core.ctx import BotContext
from core.analytics import track_event
from core.state import StateStore
from core.texts.ru import (
    ONBOARDING_DONE_FALLBACK,
    PAY_PENDING,
    checkout_card,
    discount_offer_text,
    menu_paywall_text,
    onboarding_offer,
    pay_button,
    pay_success,
)
from core.flows.referral import friend_bonus_note
from core.ui import Button, Keyboard

BONUS_EXPIRED = "Бонус +50% к первой покупке уже закончился. Вот обычные пакеты 👇"
# Discount ladder (hypothesis 3): the backend owns the offers. ``discount:<slug>`` is both the pricing
# and the checkout context; it answers with the discounted tiers (and offer_slug) only while the push is live.
DISCOUNT_CONTEXT_PREFIX = "discount:"
# ``winback_20_299`` is the 48 h offer for lapsed buyers: same context and flow, its own wording.
WINBACK_SLUG = "winback_20_299"
DISCOUNT_SLUGS = frozenset({"gift_discount_40m", "last_chance_99", WINBACK_SLUG})
DISCOUNT_EXPIRED = "Скидка уже закончилась. Вот обычные пакеты 👇"
WINBACK_EXPIRED = "Предложение закончилось. Вот обычные пакеты 👇"


def discount_expired_text(pricing_context_or_slug: str | None) -> str:
    slug = str(pricing_context_or_slug or "").removeprefix(DISCOUNT_CONTEXT_PREFIX)
    return WINBACK_EXPIRED if slug == WINBACK_SLUG else DISCOUNT_EXPIRED


def is_discount_context(pricing_context: str | None) -> bool:
    return bool(pricing_context) and str(pricing_context).startswith(DISCOUNT_CONTEXT_PREFIX)


class PaymentBackend(Protocol):
    async def paywall_shown(self, user_id: str) -> None: ...
    async def get_price(self, user_id: str, *, context: str = "menu") -> dict: ...
    async def create_payment(
        self, user_id: str, generations: int, amount_rubles: int, *, context: str = "menu",
        operation_key: str | None = None,
    ) -> dict: ...
    async def payment_status(self, user_id: str, payment_id: str) -> dict: ...
    async def get_me(self, user_id: str) -> dict: ...


def paywall_keyboard(generations: int, rubles: int) -> Keyboard:
    return Keyboard.from_rows([
        [Button.callback(pay_button(generations, rubles), "pay")],
    ])


def _package_label(tier: dict, *, menu: bool) -> str:
    generations, bonus, rubles = int(tier["generations"]), int(tier.get("bonus_generations") or 0), int(tier["rubles"])
    if menu:
        # The menu promo bonus is a gift on top of the package: "20 + 10 🎁 · 499 ₽".
        return f"{generations} + {bonus} 🎁 · {rubles} ₽" if bonus else f"{generations} ⚡ энергии · {rubles} ₽"
    return f"{generations + bonus} ⚡ энергии · {rubles} ₽"


def package_keyboard(tiers: list[dict], *, menu: bool = False) -> Keyboard:
    """Render server-priced repeat-purchase packages without hardcoded prices."""
    return Keyboard.from_rows([
        [Button.callback(
            _package_label(tier, menu=menu),
            f"pay.tier:{int(tier['generations'])}:{int(tier['rubles'])}",
        )]
        for tier in tiers
        if int(tier.get("generations") or 0) > 0 and int(tier.get("rubles") or 0) > 0
    ])


OFFER_CONTEXTS = frozenset({
    "onboarding_trial", "onboarding_bonus", "onboarding_paywall", "first_payment_discount",
})


# T-Bank payment: the backend sends the ways to pay as link buttons (``pay_buttons``: method, text, url), SberPay first.
# A dot in the colour of the bank's button marks the wallet, the card has its own mark.
PAY_BUTTON_MARKS = {"sberpay": "🟢", "sbp": "⚪", "tpay": "🟡", "card": "💳"}  # --- T-BANK ---
PAY_BUTTONS_PER_ROW = 2  # --- T-BANK ---


# --- T-BANK --- new: checkout_pay_buttons
def checkout_pay_buttons(payment: dict) -> list[tuple[str, str]]:
    """(text, url) of the buttons of a payment, in the backend's order; [] when it sent none (YooKassa, old backend):
    then the single «Оплатить» button leads to ``confirmation_url`` as before."""
    result = []
    for item in payment.get("pay_buttons") or []:
        if not isinstance(item, dict):
            continue
        text, url = str(item.get("text") or "").strip(), str(item.get("url") or "")
        if not text or not url.lower().startswith("https://"):
            continue
        mark = PAY_BUTTON_MARKS.get(str(item.get("method")))
        result.append((f"{mark} {text}" if mark else text, url))
    return result
# --- /T-BANK ---


# --- T-BANK --- new: pay_button_rows
def pay_button_rows(buttons: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """The buttons two in a row: SberPay · SBP / T-Pay · card."""
    return [buttons[i:i + PAY_BUTTONS_PER_ROW] for i in range(0, len(buttons), PAY_BUTTONS_PER_ROW)]
# --- /T-BANK ---


# --- T-BANK --- changed: payment_link_keyboard
def payment_link_keyboard(
    url: str, rubles: int | None = None, *, pricing_context: str | None = None,
    pay_buttons: list[tuple[str, str]] | None = None,
) -> Keyboard:
    # No manual "check payment" button: the webhook (backed by a recovery
    # sweep) always credits and notifies on its own. The back button mirrors
    # Telegram's checkout card.
    rows = [
        [Button.link(text, link) for text, link in row] for row in pay_button_rows(pay_buttons or [])
    ] or [[Button.link(f"Оплатить {rubles} ₽" if rubles else "Оплатить", url)]]
    if pricing_context is not None:
        back = (
            "← К предложению" if pricing_context in OFFER_CONTEXTS or is_discount_context(pricing_context)
            else "← Выбрать другой пакет"
        )
        rows.append([Button.callback(back, "pay.back")])
    return Keyboard.from_rows(rows)
# --- /T-BANK ---


async def show_paywall(
    context: BotContext, state: StateStore, backend: PaymentBackend, *,
    pricing_context: str = "onboarding_paywall",
    continuation: str | None = None,
    extra_keyboard: Keyboard | None = None,
    notify_backend: bool = True,
) -> None:
    """``notify_backend=False``: the onboarding hook screen already started the paywall ladder."""
    expired_context = pricing_context
    try:
        if notify_backend and pricing_context in {"onboarding_trial", "onboarding_bonus"}:
            await backend.paywall_shown(context.user.external_id)
        price = await backend.get_price(context.user.external_id, context=pricing_context)
        friend_note = friend_bonus_note(price.get("referral_friend_bonus"))
        tiers = list(price.get("tiers") or [])
        # Keep old backend adapters usable during the rollout; production MAX
        # returns the same shape as the shared /payments/price endpoint.
        if not tiers and "generations" in price and "rubles" in price:
            tiers = [{"generations": price["generations"], "rubles": price["rubles"]}]
        valid_tiers = [
            {
                "generations": int(tier["generations"]),
                "rubles": int(tier["rubles"]),
                **(
                    {"original_rubles": int(tier["original_rubles"])}
                    if int(tier.get("original_rubles") or 0) > int(tier["rubles"])
                    else {}
                ),
                **({"bonus_generations": int(tier["bonus_generations"])}
                   if int(tier.get("bonus_generations") or 0) > 0 else {}),
            }
            for tier in tiers
            if int(tier.get("generations") or 0) > 0 and int(tier.get("rubles") or 0) > 0
        ]
        if not valid_tiers:
            raise ValueError("No payment tiers available")
        if pricing_context == "onboarding_bonus" and not all(
            tier.get("bonus_generations") for tier in valid_tiers
        ):
            # The backend closed the bonus window (it returns ordinary menu
            # packages then); check out as a menu purchase or it would refuse.
            pricing_context = "menu"
        discount_over = is_discount_context(pricing_context) and (
            price.get("offer_slug") != pricing_context.removeprefix(DISCOUNT_CONTEXT_PREFIX)
        )
        if discount_over:
            # The offer is over (the backend returned the ordinary packages): say so, check out as a menu purchase.
            expired_context = pricing_context
            pricing_context = "menu"
    except Exception:
        await state.update(
            context.user, step="paywall", payment_context=pricing_context,
            payment_continuation=continuation, payment_operation_key=None,
        )
        await context.send_text(ONBOARDING_DONE_FALLBACK)
        return
    if discount_over:
        await context.send_text(discount_expired_text(expired_context))
    if is_discount_context(pricing_context):
        await track_event(context, "discount_offer_shown", {
            "slug": pricing_context.removeprefix(DISCOUNT_CONTEXT_PREFIX), "tiers_count": len(valid_tiers),
        })
        await state.update(
            context.user, step="paywall", payment_context=pricing_context,
            offers=valid_tiers, payment_continuation=continuation, payment_operation_key=None,
        )
        await context.show_keyboard_screen(
            state, text=_with_friend_note(
                discount_offer_text(valid_tiers, slug=pricing_context.removeprefix(DISCOUNT_CONTEXT_PREFIX)),
                friend_note,
            ),
            keyboard=_append_keyboard(package_keyboard(valid_tiers), extra_keyboard),
        )
        return
    if pricing_context == "menu":
        await track_event(context, "paywall_shown", {
            "source": "menu",
            "tiers_count": len(valid_tiers),
        })
        await state.update(
            context.user, step="paywall", payment_context=pricing_context,
            offers=valid_tiers, payment_continuation=continuation, payment_operation_key=None,
        )
        await context.show_keyboard_screen(
            state,
            text=_with_friend_note(menu_paywall_text(valid_tiers, rich=context.user.platform != "max"), friend_note),
            keyboard=_append_keyboard(package_keyboard(valid_tiers, menu=True), extra_keyboard),
        )
        return
    if pricing_context == "onboarding_bonus":
        offers = valid_tiers
        await track_event(context, "onboarding_bonus_offer_shown", {"tiers_count": len(offers)})
        await state.update(
            context.user, step="paywall", payment_context=pricing_context,
            offers=offers, payment_continuation=continuation, payment_operation_key=None,
        )
        if all(tier.get("bonus_generations") for tier in offers):
            lines = [
                f"<b>{tier['generations'] + tier['bonus_generations']} ⚡ энергии · {tier['rubles']} ₽</b>\n"
                f"{tier['generations']} оплачиваешь + {tier['bonus_generations']} бонусом"
                for tier in offers
            ]
            text = ("<b>К первой покупке +50% ⚡ энергии</b>\n\n"
                    + "\n\n".join(lines)
                    + "\n\nБонус сразу на баланс.\nБез подписки: карта не привязывается, ничего лишнего не спишем 💛\n⏳ Только 24 часа.")
        else:
            # The backend retired the first-purchase bonus on 2026-09-24 and now
            # returns plain tiers here; never promise a bonus it will not credit.
            lines = [f"<b>{tier['generations']} ⚡ энергии · {tier['rubles']} ₽</b>" for tier in offers]
            text = ("<b>Выбери пакет</b>\n\n"
                    + "\n".join(lines)
                    + "\n\nБез подписки: карта не привязывается, ничего лишнего не спишем 💛")
        await context.show_keyboard_screen(
            state, text=_with_friend_note(text, friend_note),
            keyboard=_append_keyboard(package_keyboard(offers), extra_keyboard),
        )
        return
    generations, rubles = valid_tiers[0]["generations"], valid_tiers[0]["rubles"]
    if pricing_context == "first_payment_discount":
        await track_event(context, "first_payment_discount_shown")
        await state.update(
            context.user, step="paywall", payment_context=pricing_context,
            offer={"generations": generations, "rubles": rubles},
            payment_continuation=continuation, payment_operation_key=None,
        )
        await context.show_keyboard_screen(
            state,
            text=(f"<b>Спасибо за покупку! 🎉</b>\n\n"
                  f"Специально для тебя: <b>{generations} ⚡ энергии за {rubles} ₽</b> — "
                  "чтобы попробовать ещё больше образов в YOUR_BRAND."),
            keyboard=_append_keyboard(paywall_keyboard(generations, rubles), extra_keyboard),
        )
        return
    await track_event(context, "onboarding_offer_shown", onboarding_v2.event_properties(user_id=context.user.external_id))
    await state.update(
        context.user, step="paywall", payment_context=pricing_context,
        offer={"generations": generations, "rubles": rubles},
        payment_continuation=continuation, payment_operation_key=None,
    )
    offer_text = _with_friend_note(onboarding_offer(generations, rubles), friend_note)
    offer_keyboard = _append_keyboard(paywall_keyboard(generations, rubles), extra_keyboard)
    # v2: the paywall that follows the first free result carries a picture above the same text and buttons.
    paywall_image = (
        await onboarding_v2.image(onboarding_v2.PAYWALL_IMAGE_ENV)
        if onboarding_v2.enabled(context.user.external_id) and pricing_context == "onboarding_trial" else None
    )
    if paywall_image is not None:
        await context.show_keyboard_screen(
            state, photo=paywall_image, caption=offer_text, keyboard=offer_keyboard,
        )
        return
    await context.show_keyboard_screen(state, text=offer_text, keyboard=offer_keyboard)


def _with_friend_note(text: str, note: str) -> str:
    return f"{text}\n\n{note}" if note else text


async def open_bonus_tier(
    context: BotContext, state: StateStore, backend: PaymentBackend, total_energy: int, *,
    extra_keyboard: Keyboard | None = None,
) -> None:
    """Go straight to checkout of the +50% package a push button named.

    ``total_energy`` is the number printed on the button (paid + bonus, so 15 for
    10 + 5). Anything that does not resolve to a live bonus package falls back to
    the ordinary bonus screen instead of a dead button.
    """
    try:
        price = await backend.get_price(context.user.external_id, context="onboarding_bonus")
        tier = next((
            {
                "generations": int(item["generations"]),
                "rubles": int(item["rubles"]),
                "bonus_generations": int(item["bonus_generations"]),
            }
            for item in price.get("tiers") or []
            if int(item.get("bonus_generations") or 0) > 0
            and int(item["generations"]) + int(item["bonus_generations"]) == total_energy
        ), None)
    except Exception:
        tier = None
    if tier is None:
        await show_paywall(
            context, state, backend, pricing_context="onboarding_bonus", extra_keyboard=extra_keyboard,
        )
        return
    await track_event(context, "onboarding_bonus_offer_shown", {"tiers_count": 1})
    await state.update(
        context.user, step="paywall", payment_context="onboarding_bonus",
        offers=[tier], payment_continuation=None, payment_operation_key=None,
    )
    await handle_callback(context, state, backend, f"pay.tier:{tier['generations']}:{tier['rubles']}")


async def open_discount_tier(
    context: BotContext, state: StateStore, backend: PaymentBackend, slug: str, generations: int | None,
) -> None:
    """Go straight to checkout of the package a discount push button named (``gift.pay:<slug>:<generations>``).

    The backend prices the offer and only answers with it while the push is live for this user. Anything
    else (expired, used, somebody else's, an unknown package) says the discount is over and shows the
    ordinary packages instead of a dead button.
    """
    try:
        price = await backend.get_price(context.user.external_id, context=f"{DISCOUNT_CONTEXT_PREFIX}{slug}")
        live = price.get("offer_slug") == slug
        tiers = [
            {
                "generations": int(item["generations"]), "rubles": int(item["rubles"]),
                **({"original_rubles": int(item["original_rubles"])}
                   if int(item.get("original_rubles") or 0) > int(item["rubles"]) else {}),
            }
            for item in price.get("tiers") or []
        ]
    except Exception:
        live, tiers = False, []
    tier = next((item for item in tiers if item["generations"] == generations), None)
    if not live or not tiers or (tier is None and generations is not None):
        await context.send_text(discount_expired_text(slug))
        await show_paywall(context, state, backend, pricing_context="menu")
        return
    tier = tier or tiers[0]
    await track_event(context, "discount_offer_shown", {"slug": slug, "tiers_count": 1})
    await state.update(
        context.user, step="paywall", payment_context=f"{DISCOUNT_CONTEXT_PREFIX}{slug}",
        offers=[tier], payment_continuation=None, payment_operation_key=None,
    )
    await handle_callback(context, state, backend, f"pay.tier:{tier['generations']}:{tier['rubles']}")


def _append_keyboard(base: Keyboard, extra: Keyboard | None) -> Keyboard:
    if extra is None:
        return base
    return Keyboard.from_rows([*base.rows, *extra.rows])


# --- T-BANK --- changed: handle_callback
async def handle_callback(
    context: BotContext, state: StateStore, backend: PaymentBackend, payload: str,
    *, success_keyboard: Keyboard | None = None,
) -> bool:
    current = dict(await state.get(context.user))
    if payload == "pay.back":
        # "← К предложению" on the payment card: back to the same offer.
        if current.get("step") != "paywall":
            return False
        await context.answer_callback()
        await show_paywall(
            context, state, backend,
            pricing_context=str(current.get("payment_context") or "menu"),
            continuation=current.get("payment_continuation"),
        )
        return True
    if payload == "pay" or payload.startswith("pay.tier:"):
        offer = current.get("offer") or {}
        pricing_context = str(current.get("payment_context") or "onboarding_paywall")
        if payload.startswith("pay.tier:"):
            try:
                _, generations_raw, rubles_raw = payload.split(":")
                offer = {"generations": int(generations_raw), "rubles": int(rubles_raw)}
            except ValueError:
                await context.send_text("⚠️ Не удалось выбрать пакет. Открой покупку ещё раз.")
                return True
            offers = current.get("offers") or []
            if not any(
                int(known.get("generations") or 0) == offer["generations"]
                and int(known.get("rubles") or 0) == offer["rubles"]
                for known in offers
            ):
                await context.send_text("⚠️ Этот пакет уже недоступен. Открой покупку ещё раз.")
                return True
        try:
            selected_generations = int(offer.get("generations") or 0)
            selected_rubles = int(offer.get("rubles") or 0)
            operation_key = str(current.get("payment_operation_key") or uuid.uuid4().hex)
            await state.update(context.user, payment_operation_key=operation_key)
            if not offer:
                price = await backend.get_price(context.user.external_id, context=pricing_context)
                tiers = price.get("tiers") or [price]
                offer = {"generations": int(tiers[0]["generations"]), "rubles": int(tiers[0]["rubles"])}
                selected_generations = int(offer["generations"])
                selected_rubles = int(offer["rubles"])
            payment = await backend.create_payment(
                context.user.external_id, int(offer["generations"]), int(offer["rubles"]),
                context=pricing_context, operation_key=operation_key,
            )
        except Exception:
            if pricing_context == "onboarding_bonus":
                # A bonus screen left open past the 24h window: say so and
                # show the ordinary packages instead of a dead end.
                await context.answer_callback()
                await context.send_text(BONUS_EXPIRED)
                await show_paywall(context, state, backend, pricing_context="menu")
                return True
            if is_discount_context(pricing_context):
                # The discount ended while the screen was open: say so and show the ordinary packages.
                await context.answer_callback()
                await context.send_text(discount_expired_text(pricing_context))
                await show_paywall(context, state, backend, pricing_context="menu")
                return True
            await context.answer_callback("Не получилось создать платёж, попробуй ещё раз")
            await context.send_text("⚠️ Не получилось создать платёж. Попробуй ещё раз.")
            return True
        if pricing_context in {"onboarding_trial", "onboarding_bonus"} or is_discount_context(pricing_context):
            await track_event(context, "onboarding_pay_clicked")
        await state.update(
            context.user,
            payment_id=str(payment["yookassa_id"]),
            offer={"generations": selected_generations, "rubles": selected_rubles},
        )
        await context.answer_callback()
        # The +50% offer shows what its screen promised; the menu promo bonus is the one the backend
        # fixed on this payment at checkout.
        bonus_generations = next((
            int(known.get("bonus_generations") or 0)
            for known in (current.get("offers") or [])
            if int(known.get("generations") or 0) == selected_generations
            and int(known.get("rubles") or 0) == selected_rubles
        ), 0) if pricing_context == "onboarding_bonus" else (
            int(payment.get("bonus_generations") or 0) if pricing_context == "menu" else 0
        )
        # A live offer shows the ordinary price it replaces («20 ⚡ · 299 ₽ (вместо 499)»).
        original_rubles = next((
            int(known.get("original_rubles") or 0)
            for known in (current.get("offers") or [])
            if int(known.get("generations") or 0) == selected_generations
            and int(known.get("rubles") or 0) == selected_rubles
        ), 0) if is_discount_context(pricing_context) else 0
        await context.send_text(
            checkout_card(selected_generations, selected_rubles, bonus_generations, original_rubles),
            keyboard=payment_link_keyboard(
                payment["confirmation_url"], selected_rubles, pricing_context=pricing_context,
                pay_buttons=checkout_pay_buttons(payment),
            ),
        )
        await track_event(context, "payment_link_sent", {
            "amount_rubles": selected_rubles,
            "generations": selected_generations,
            "yookassa_id": str(payment["yookassa_id"]),
            "source": pricing_context,
        })
        return True
    if payload != "pay_check" and not payload.startswith("pay_check:"):
        return False
    # New payment messages carry their own ID.  Older messages retain the
    # legacy behaviour so they do not become dead controls after a release.
    payment_id = (
        payload.removeprefix("pay_check:")
        if payload.startswith("pay_check:") else current.get("payment_id")
    )
    if not payment_id:
        await context.answer_callback("Нет активного платежа")
        return True
    try:
        status = await backend.payment_status(context.user.external_id, str(payment_id))
    except Exception:
        await context.answer_callback("Не удалось проверить, попробуй позже")
        await context.send_text("⚠️ Не удалось проверить оплату. Попробуй ещё раз через минуту.")
        return True
    if not status.get("paid"):
        if str(status.get("status") or "").lower() in {"canceled", "cancelled", "expired"}:
            await context.answer_callback("Платёж отменён или истёк")
            # The old operation key would otherwise return the same dead link.
            await state.update(context.user, payment_id=None, payment_operation_key=None)
            await show_paywall(
                context, state, backend,
                pricing_context=str(current.get("payment_context") or "menu"),
                continuation=current.get("payment_continuation"),
            )
            return True
        await context.answer_callback("Оплата ещё не прошла")
        await context.send_text(PAY_PENDING)
        return True
    balance = status.get("new_balance")
    if balance is None:
        balance = (await backend.get_me(context.user.external_id)).get("generations_remaining", 0)
    # A message button can outlive the payment screen. It may report a real
    # payment, but it must never erase a newer generation/profile flow.
    active_payment_id = current.get("payment_id")
    # A success button can live far longer than its paywall. Only the current
    # paywall is allowed to change state: a receipt for an old payment must not
    # interrupt a new generation, onboarding, or profile flow.
    if current.get("step") != "paywall" or active_payment_id not in {None, payment_id}:
        await context.answer_callback("Оплата уже подтверждена")
        await context.send_text("Оплата по этой ссылке уже подтверждена ✅")
        return True
    continuation = current.get("payment_continuation")
    # The first purchase ends the closed onboarding funnel.
    updates = {"step": "done", "payment_continuation": None, "onboarding": None}
    if current.get("payment_id") == payment_id:
        updates["payment_id"] = None
    await state.update(context.user, **updates)
    await context.answer_callback()
    rows: list[list[Button]] = []
    if isinstance(continuation, str) and continuation:
        rows.append([Button.callback("Продолжить", continuation)])
    if success_keyboard is not None:
        rows.extend([list(row) for row in success_keyboard.rows])
    keyboard = Keyboard.from_rows(rows) if rows else None
    success_text = (
        f"Оплата прошла ✅\n\nНа балансе: {int(balance)} ⚡ энергии.\nВыбери, что создать."
        if context.user.platform == "max" else pay_success(int(balance))
    )
    if keyboard is None:
        await context.send_text(success_text)
    else:
        await context.show_keyboard_screen(
            state, text=success_text, keyboard=keyboard,
        )
    await track_event(context, "payment_confirmed", {
        "yookassa_id": str(payment_id),
        "generations": int((current.get("offer") or {}).get("generations") or 0),
    })
    return True
# --- /T-BANK ---
