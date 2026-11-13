import logging
import re

import aiohttp
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from aiogram.fsm.context import FSMContext

from app.api.backend import backend
from app import config
from core import brand, onboarding_v2
from app.keyboards.common import get_main_menu_keyboard, get_main_miniapp_button
from app.services.analytics_sdk import AnalyticsClient
from app.states.photo import LavaCheckoutStates, PhotoUploadStates
from core.flows.payment import checkout_pay_buttons, pay_button_rows  # --- T-BANK ---
from core.flows.referral import friend_bonus_note
from core.texts.trend import generations_label

logger = logging.getLogger(__name__)
router = Router()
# The foreign-card (Robokassa) checkout is retired — it converted at ~1% and
# funnelled users into a dead end. The backend refuses new Robokassa invoices;
# this hides the entry point in every payment surface (menu + onboarding).
SHOW_FOREIGN_CARD_OPTION = False
# Robokassa rejects international-card operations below this amount.
FOREIGN_CARD_MIN_RUBLES = config.ROBOKASSA_MIN_RUBLES


async def _delete_payment_card(message, yookassa_id: str) -> None:
    """Best-effort removal of a payment card that is no longer actionable."""
    try:
        await message.delete()
    except Exception:
        logger.warning("Не удалось удалить сообщение оплаты ЮKassa: %s", yookassa_id, exc_info=True)


async def _render_checkout_message(
    message,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    replace: bool = False,
    parse_mode: str | None = None,
):
    """Keep checkout to one bot message; fall back only when it cannot be edited."""
    kwargs = {"reply_markup": keyboard}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if replace:
        if message.photo and onboarding_v2.enabled(message.chat.id):
            # v2: the offer is a picture with a caption, which a text screen cannot replace in place.
            try:
                await message.delete()
            except Exception:
                logger.debug("Could not delete the offer picture before the checkout screen", exc_info=True)
            return await message.answer(text, **kwargs)
        try:
            await message.edit_text(text, **kwargs)
            return message
        except Exception:
            logger.warning("Не удалось обновить экран оплаты, создаю новое сообщение", exc_info=True)
    return await message.answer(text, **kwargs)


PAYMENT_FLOW = {
    "m": ("menu", None),
    "o": ("onboarding_paywall", None),
    "ot": ("onboarding_trial", None),
    "ob": ("onboarding_bonus", None),
    "r1": ("menu", None),
    "r2": ("menu", None),
    "r3": ("menu", None),
    "fp": ("menu", None),
    # Discount ladder (hypothesis 3): the context names the push whose offer the backend checks.
    "d1": ("discount:gift_discount_40m", "gift_discount_40m"),
    "d2": ("discount:last_chance_99", "last_chance_99"),
    # The 48 h offer for lapsed buyers (20 ⚡ for 299 ₽): same checkout, its own wording.
    "d3": ("discount:winback_20_299", "winback_20_299"),
}
DISCOUNT_FLOW_CODES = {"gift_discount_40m": "d1", "last_chance_99": "d2", "winback_20_299": "d3"}
DISCOUNT_EXPIRED_TEXT = "Скидка уже закончилась. Вот обычные пакеты 👇"
WINBACK_SLUG = "winback_20_299"
WINBACK_EXPIRED_TEXT = "Предложение закончилось. Вот обычные пакеты 👇"
# Ordinary price of the 20 ⚡ package, shown crossed out; the price charged always comes from the backend.
WINBACK_LIST_RUBLES = 499


def _discount_expired_text(slug: str | None) -> str:
    return WINBACK_EXPIRED_TEXT if slug == WINBACK_SLUG else DISCOUNT_EXPIRED_TEXT


def _flow_code(source: str, offer_code: str | None) -> str:
    if source == "discount":
        return DISCOUNT_FLOW_CODES.get(offer_code or "", "m")
    if source == "onboarding":
        return "o"
    if source == "onboarding_trial":
        return "ot"
    if source == "onboarding_bonus":
        return "ob"
    if offer_code == "onboarding_paywall_reminder_1":
        return "r1"
    if offer_code == "onboarding_paywall_reminder_2":
        return "r2"
    if offer_code == "onboarding_paywall_reminder_3":
        return "r3"
    if offer_code == "first_payment_discount":
        return "fp"
    return "m"


def _stars_payment_context(source: str, offer_code: str | None = None) -> str:
    """Map a checkout source to the backend payment context (mirror of _create_and_send_payment)."""
    if source == "discount":
        return f"discount:{offer_code}"
    if source == "onboarding_trial":
        return "onboarding_trial"
    if source == "onboarding":
        return "onboarding_paywall"
    if source == "onboarding_bonus":
        return "onboarding_bonus"
    if source in ("menu", "menu_tier"):
        return "menu"
    return "special_offer"


def _stars_button(generations: int, rubles: int, flow: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="⭐ Оплатить звёздами Telegram",
        callback_data=f"pay_s_{generations}_{rubles}_{flow}",
    )


def _lava_button(generations: int, rubles: int, flow: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="🌍 Карта не из РФ ($)",
        callback_data=f"pay_l_{generations}_{rubles}_{flow}",
    )


def _onboarding_trial_offer_text(generations: int, rubles: int) -> str:
    return (
        "😍 <b>Вау, твой первый образ готов!</b>\n\n"
        "Это только проба. Дальше можно создать ещё.\n\n"
        f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n"
        "Без подписки: карта не привязывается, ничего лишнего не спишем 💛"
    )


BONUS_EXPIRED_TEXT = "Бонус +50% к первой покупке уже закончился. Вот обычные пакеты 👇"


def _bonus_generations(generations: int) -> int:
    """Display copy of the backend rule: +50% on the 10 and 20 packages."""
    return generations // 2 if generations in (10, 20) else 0


def _onboarding_bonus_offer_text(tiers: list[dict]) -> str:
    lines = [
        f"<b>{tier['generations'] + tier['bonus_generations']} ⚡ энергии · {tier['rubles']} ₽</b>\n"
        f"{tier['generations']} оплачиваешь + {tier['bonus_generations']} бонусом"
        for tier in tiers
    ]
    return (
        "🎁 <b>К первой покупке +50% ⚡ энергии</b>\n\n"
        + "\n\n".join(lines)
        + "\n\nБонус сразу на баланс.\nБез подписки: карта не привязывается, ничего лишнего не спишем 💛\n⏳ Действует 24 часа."
    )


def _onboarding_bonus_offer_keyboard(tiers: list[dict], telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{tier['generations'] + tier['bonus_generations']} ⚡ энергии · {tier['rubles']} ₽",
            callback_data=f"buy_onboarding_bonus_tier_{tier['generations']}_{tier['rubles']}",
        )]
        for tier in tiers
    ] + [[get_main_miniapp_button("Посмотреть возможности ✨", telegram_id)]])


def _onboarding_trial_offer_keyboard(generations: int, rubles: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"{generations} ⚡ энергии · {rubles} ₽",
            callback_data=f"buy_onboarding_trial_tier_{generations}_{rubles}",
        )
    ]])


def _has_alternative_payment_methods(telegram_id: int, source: str = "menu") -> bool:
    """Whether the method screen offers anything besides YooKassa."""
    if source == "discount":
        # A discount is paid by card/SBP or Stars; the backend refuses it on the foreign-card providers.
        return config.STARS_PAYMENTS_ENABLED
    foreign_card = SHOW_FOREIGN_CARD_OPTION and (
        not config.ROBOKASSA_TEST_MODE or telegram_id in config.MINI_APP_USER_IDS
    )
    return foreign_card or config.LAVA_PAYMENTS_ENABLED or config.STARS_PAYMENTS_ENABLED


async def _show_payment_method_choice(
    message,
    *,
    telegram_id: int,
    generations: int,
    rubles: int,
    source: str = "menu",
    offer_code: str | None = None,
    replace: bool = False,
    analytics: AnalyticsClient | None = None,
) -> None:
    """Let the customer choose the issuer region before a provider is created.

    With YooKassa as the only provider there is nothing to choose: open its
    checkout straight away instead of a one-button screen.
    """
    flow = _flow_code(source, offer_code)
    if not _has_alternative_payment_methods(telegram_id, source):
        if analytics:
            await analytics.track("payment_method_choice_shown", user_id=str(telegram_id), properties={
                "generations": generations,
                "rubles": rubles,
                "source": source,
                "offer_code": offer_code,
                "skipped": True,
            })
        await _create_and_send_payment(
            message, telegram_id, generations, rubles, analytics,
            source=source, offer_code=offer_code, replace=replace,
        )
        return
    buttons = [
        [InlineKeyboardButton(
            text="🇷🇺 Карта РФ или СБП",
            callback_data=f"pay_y_{generations}_{rubles}_{flow}",
        )],
    ]
    if SHOW_FOREIGN_CARD_OPTION and source != "discount" and (
        not config.ROBOKASSA_TEST_MODE or telegram_id in config.MINI_APP_USER_IDS
    ):
        foreign_card_rubles = max(rubles, FOREIGN_CARD_MIN_RUBLES)
        foreign_card_generations = (
            config.ROBOKASSA_MIN_GENERATIONS
            if rubles < FOREIGN_CARD_MIN_RUBLES
            else generations
        )
        buttons.append([InlineKeyboardButton(
            text=f"🌍 Зарубежная карта · {foreign_card_generations} ген. · {foreign_card_rubles} ₽",
            callback_data=f"pay_r_{generations}_{rubles}_{flow}",
        )])
    stars_hint = ""
    if config.LAVA_PAYMENTS_ENABLED and source != "discount":
        buttons.append([_lava_button(generations, rubles, flow)])
        stars_hint = "\n\n<i>🌍 Карта не из РФ — оплата в долларах картой любой страны.</i>"
    if config.STARS_PAYMENTS_ENABLED:
        buttons.append([_stars_button(generations, rubles, flow)])
        if not stars_hint:
            stars_hint = "\n\n<i>⭐ Звёзды Telegram — если у тебя карта не из РФ.</i>"
    is_onboarding = source in ("onboarding", "onboarding_trial", "onboarding_bonus", "discount")
    if is_onboarding:
        buttons.append([get_main_miniapp_button("Посмотреть возможности ✨", telegram_id)])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\nВыбери способ оплаты."
    if source == "onboarding":
        text = (
            f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\n"
            "Выбери способ оплаты.\n"
            "Без подписки: карта не привязывается, ничего лишнего не спишем 💛"
        )
    elif source == "onboarding_bonus" and _bonus_generations(generations):
        bonus = _bonus_generations(generations)
        text = (
            f"<b>{generations + bonus} ⚡ энергии · {rubles} ₽</b>\n"
            f"{generations} + {bonus} бонусом\n\n"
            "Выбери способ оплаты.\n"
            "Без подписки: карта не привязывается, ничего лишнего не спишем 💛"
        )
    elif source == "onboarding_trial":
        text = (
            f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\n"
            "Стартовый пакет. Выбери способ оплаты.\n"
            "Без подписки: карта не привязывается, ничего лишнего не спишем 💛"
        )
    elif source == "discount" and offer_code == WINBACK_SLUG:
        text = (
            f"<b>{generations} ⚡ энергии · {rubles} ₽ (вместо {WINBACK_LIST_RUBLES})</b>\n"
            "Только для вас на 48 часов\n\n"
            "Выбери способ оплаты.\n"
            "Без подписки: карта не привязывается, ничего лишнего не спишем 💛"
        )
    elif source == "discount":
        text = (
            f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n"
            "Скидка на первую покупку\n\n"
            "Выбери способ оплаты.\n"
            "Без подписки: карта не привязывается, ничего лишнего не спишем 💛"
        )
    text += stars_hint
    if SHOW_FOREIGN_CARD_OPTION and source != "discount" and rubles < FOREIGN_CARD_MIN_RUBLES:
        text += (
            f"\n\n<i>Для зарубежной карты: {config.ROBOKASSA_MIN_GENERATIONS} ⚡ энергии "
            f"за {FOREIGN_CARD_MIN_RUBLES} ₽.</i>"
        )
    await _render_checkout_message(
        message,
        text,
        keyboard,
        replace=replace,
        parse_mode="HTML",
    )

    # This is where the customer first sees the price, and where most of them
    # stop. Without it the step between the paywall click and the payment link
    # is invisible in analytics.
    if analytics:
        await analytics.track("payment_method_choice_shown", user_id=str(telegram_id), properties={
            "generations": generations,
            "rubles": rubles,
            "source": source,
            "offer_code": offer_code,
        })


def _tier_description(generations: int) -> str:
    """Return a clear use case for each package in the menu overview."""
    if generations <= 10:
        return "Отличный старт, чтобы попробовать и оценить результат. 🔥"
    if generations <= 20:
        return "Для пары фотосессий и новых образов."
    if generations <= 50:
        return "Оптимальный выбор: больше свободы и выгодная цена. ✨"
    if generations <= 100:
        return "Больше ⚡ энергии для новых образов и фотосессий. 💎"
    return "Для тех, кто хочет создавать фото без ограничений. 🚀"


def _format_tier_offer(tier: dict) -> str:
    """Format a package overview from the current server-side price."""
    generations = int(tier["generations"])
    rubles = int(tier["rubles"])
    original_rubles = int(tier.get("original_rubles") or 0)

    bonus = int(tier.get("bonus_generations") or 0)
    price_line = f"{generations} ⚡ энергии — <b>{rubles} ₽</b>"
    if bonus:
        price_line = f"{generations} ⚡ + {bonus} в подарок 🎁 — <b>{rubles} ₽</b>"
    if original_rubles > rubles:
        price_line += f" вместо <s>{original_rubles} ₽</s>"
    return f"{price_line}\n{_tier_description(generations)}"


# --- T-BANK --- changed: _create_and_send_payment
async def _create_and_send_payment(
    message,
    telegram_id: int,
    generations: int,
    rubles: int,
    analytics: AnalyticsClient | None = None,
    source: str = "menu",
    offer_code: str | None = None,
    replace: bool = False,
):
    """Create YooKassa payment and send payment link to user."""
    payment_context = (
        "onboarding_trial" if source == "onboarding_trial"
        else "onboarding_paywall" if source == "onboarding"
        else "onboarding_bonus" if source == "onboarding_bonus"
        else _stars_payment_context(source, offer_code) if source == "discount"
        else "menu"
    )
    if source not in {"menu", "menu_tier", "onboarding", "onboarding_trial", "onboarding_bonus", "discount"}:
        payment_context = "special_offer"

    try:
        result = await backend.create_yookassa_payment(
            telegram_id=telegram_id,
            generations=generations,
            amount_rubles=rubles,
            context=payment_context,
            offer_code=offer_code,
        )
    except Exception as e:
        logger.error(f"Ошибка создания платежа ЮКасса: {e}")
        if source == "onboarding_bonus":
            # A bonus package tapped after the 24h window: the backend refuses
            # it. Say so and show ordinary packages instead of a dead end.
            await message.answer(BONUS_EXPIRED_TEXT)
            await start_payment_flow(message, telegram_id, analytics=analytics)
            return
        if source == "discount":
            await message.answer(_discount_expired_text(offer_code))
            await start_payment_flow(message, telegram_id, analytics=analytics)
            return
        if offer_code:
            await message.answer("Этот оффер уже недоступен. Выбери актуальный пакет в меню.")
        else:
            await message.answer("⚠️ Не удалось создать платёж. Попробуй позже.")
        return

    confirmation_url = result.get("confirmation_url")
    yookassa_id = result.get("yookassa_id")
    if offer_code:
        generations = int(result["generations"])
        rubles = int(result["amount_rubles"])

    if not confirmation_url:
        await message.answer("⚠️ Не удалось получить ссылку на оплату.")
        return

    back_button = InlineKeyboardButton(
        text="← Выбрать пакет",
        callback_data="onboarding_pay",
    ) if source == "onboarding" else InlineKeyboardButton(
        text="← К предложению",
        callback_data="onboarding_trial_offer",
    ) if source == "onboarding_trial" else InlineKeyboardButton(
        text="← К предложению",
        callback_data="onboarding_bonus_offer",
    ) if source == "onboarding_bonus" else InlineKeyboardButton(
        text="← Выбрать другой пакет",
        callback_data="back_to_packages",
    )
    # T-Bank: SberPay · SBP · T-Pay · card instead of the single button (the backend sends ``pay_buttons`` only then).
    pay_rows = [
        [InlineKeyboardButton(text=text, url=link) for text, link in row]
        for row in pay_button_rows(checkout_pay_buttons(result))
    ] or [[InlineKeyboardButton(text=f"Оплатить {rubles} ₽", url=confirmation_url)]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[*pay_rows, [back_button]])
    package_label = f"{generations} ⚡ энергии · {rubles} ₽"
    if source == "discount" and offer_code == WINBACK_SLUG:
        package_label += f" (вместо {WINBACK_LIST_RUBLES})"
    bonus_detail = ""
    # The menu promo bonus is fixed by the backend at checkout and echoed back.
    bonus = (
        _bonus_generations(generations) if source == "onboarding_bonus"
        else int(result.get("bonus_generations") or 0)
    )
    if bonus:
        package_label = f"{generations + bonus} ⚡ энергии · {rubles} ₽"
        bonus_detail = f"{generations} + {bonus} бонусом — бонус придёт на баланс вместе с оплатой.\n"

    payment_message = await _render_checkout_message(
        message,
        f"<b>{package_label}</b>\n{bonus_detail}\n"
        "Оплата картой РФ или СБП через ЮKassa.\n\n"
        + brand.consent_html(),
        keyboard,
        replace=replace,
        parse_mode="HTML",
    )

    try:
        saved_payment = await backend.save_yookassa_payment_message(
            telegram_id, yookassa_id, payment_message.message_id,
        )
        # A very fast payment can be confirmed between sending the card and
        # saving its message ID. The backend already sent its success notice,
        # so remove the now-stale card directly in that rare race.
        if saved_payment.get("status") == "succeeded":
            await _delete_payment_card(payment_message, yookassa_id)
    except Exception:
        logger.warning(
            "Не удалось сохранить сообщение оплаты ЮKassa для удаления: %s",
            yookassa_id,
            exc_info=True,
        )

    if analytics:
        await analytics.track("payment_link_sent", user_id=str(telegram_id), properties={
            "amount_rubles": rubles, "generations": generations, "yookassa_id": yookassa_id, "source": source,
        })
# --- /T-BANK ---


def _format_money(amount, currency: str) -> str:
    if amount is None:
        return ""
    symbol = {"USD": "$", "EUR": "€"}.get(currency, "")
    value = f"{float(amount):.2f}"
    return f"{symbol}{value}" if symbol else f"{value} {currency}"


LAVA_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")
LAVA_EMAIL_EXAMPLE = "<code>name@gmail.com</code>"
LAVA_EMAIL_RETRY = (
    "Похоже, в адресе ошибка 🙈\n\n"
    f"Напиши почту ещё раз, например {LAVA_EMAIL_EXAMPLE}"
)


def lava_email_prompt(generations: int | None) -> str:
    package = f": {generations_label(generations)}" if generations else ""
    return (
        f"💳 <b>Оплата{package}</b>\n"
        "Остался один шаг — почта для чека.\n\n"
        "📧 Отправь её одним сообщением, и я сразу пришлю ссылку на оплату.\n\n"
        f"Например: {LAVA_EMAIL_EXAMPLE}\n\n"
        "<i>Спросим только один раз.</i>"
    )


async def _ask_lava_email(
    message, state: FSMContext, *, checkout: dict, retry: bool = False, replace: bool = False,
) -> None:
    """Park the chosen package and ask for the e-mail lava.top requires."""
    data = await state.get_data()
    previous_state = data.get("lava_previous_state")
    if previous_state is None:
        previous_state = await state.get_state()
    await state.set_state(LavaCheckoutStates.waiting_for_email)
    await state.update_data(lava_checkout=checkout, lava_previous_state=previous_state or "")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сменить способ оплаты", callback_data="lava_email_cancel"),
    ]])
    await _render_checkout_message(
        message,
        LAVA_EMAIL_RETRY if retry else lava_email_prompt(checkout.get("generations")),
        keyboard,
        replace=replace and not retry,
        parse_mode="HTML",
    )


async def _restore_state_after_lava_email(state: FSMContext) -> dict | None:
    data = await state.get_data()
    checkout = data.get("lava_checkout")
    previous_state = data.get("lava_previous_state") or None
    await state.set_state(previous_state)
    await state.update_data(lava_checkout=None, lava_previous_state=None)
    return checkout


async def _create_and_send_lava_payment(
    message,
    telegram_id: int,
    generations: int,
    rubles: int,
    analytics: AnalyticsClient | None = None,
    source: str = "menu",
    offer_code: str | None = None,
    replace: bool = False,
    state: FSMContext | None = None,
    email: str | None = None,
):
    """Send a lava.top checkout link (USD card) for the selected package.

    lava.top needs the buyer's e-mail for its receipt: the first checkout asks
    for it (backend answers 409), later ones reuse the stored address.
    lava.top settles through its webhook; the backend then pushes the usual
    success notice and removes this card. «Я оплатил» re-checks on demand.
    """
    try:
        result = await backend.create_lava_payment(
            telegram_id=telegram_id,
            generations=generations,
            amount_rubles=rubles,
            context=_stars_payment_context(source, offer_code),
            offer_code=offer_code,
            email=email,
        )
    except aiohttp.ClientResponseError as e:
        if e.status in (400, 409) and state is not None:
            await _ask_lava_email(
                message, state,
                checkout={
                    "generations": generations, "rubles": rubles, "source": source,
                    "offer_code": offer_code,
                },
                retry=e.status == 400,
                replace=replace,
            )
            return
        logger.error("Ошибка создания платежа lava.top: %s", e)
        await message.answer(
            "⚠️ Не удалось создать платёж зарубежной картой. Попробуй ещё раз через минуту."
        )
        return
    except Exception as e:
        logger.error("Ошибка создания платежа lava.top: %s", e)
        if source == "onboarding_bonus":
            await message.answer(BONUS_EXPIRED_TEXT)
            await start_payment_flow(message, telegram_id, analytics=analytics)
            return
        await message.answer(
            "⚠️ Не удалось создать платёж зарубежной картой. Попробуй ещё раз через минуту."
        )
        return

    payment_url = result.get("payment_url")
    contract_id = result.get("contract_id")
    if not payment_url or not contract_id:
        await message.answer("⚠️ Не удалось получить ссылку на оплату.")
        return
    generations = int(result["generations"])
    price = _format_money(result.get("amount"), result.get("currency") or "USD")

    back_button = InlineKeyboardButton(
        text="← Выбрать пакет",
        callback_data="onboarding_pay",
    ) if source == "onboarding" else InlineKeyboardButton(
        text="← К предложению",
        callback_data="onboarding_trial_offer",
    ) if source == "onboarding_trial" else InlineKeyboardButton(
        text="← К предложению",
        callback_data="onboarding_bonus_offer",
    ) if source == "onboarding_bonus" else InlineKeyboardButton(
        text="← Выбрать другой пакет",
        callback_data="back_to_packages",
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить {price}".strip(), url=payment_url)],
        [InlineKeyboardButton(text="Я оплатил — проверить", callback_data=f"check_lava_{contract_id}")],
        [back_button],
    ])
    package_label = f"{generations} ⚡ энергии · {price}" if price else f"{generations} ⚡ энергии"
    bonus_detail = ""
    # The menu promo bonus is fixed by the backend at checkout and echoed back.
    bonus = (
        _bonus_generations(generations) if source == "onboarding_bonus"
        else int(result.get("bonus_generations") or 0)
    )
    if bonus:
        package_label = f"{generations + bonus} ⚡ энергии · {price}"
        bonus_detail = f"{generations} + {bonus} бонусом — бонус придёт на баланс вместе с оплатой.\n"

    payment_message = await _render_checkout_message(
        message,
        f"<b>{package_label}</b>\n{bonus_detail}\n"
        "Оплата картой любой страны через lava.top. Энергия придёт сюда сразу после оплаты.\n\n"
        + brand.consent_html(),
        keyboard,
        replace=replace,
        parse_mode="HTML",
    )

    try:
        saved = await backend.save_lava_payment_message(
            telegram_id, contract_id, payment_message.message_id,
        )
        if saved.get("status") == "succeeded":
            await _delete_payment_card(payment_message, contract_id)
    except Exception:
        logger.warning("Не удалось сохранить сообщение оплаты lava.top: %s", contract_id, exc_info=True)

    if analytics:
        await analytics.track("payment_link_sent", user_id=str(telegram_id), properties={
            "method": "lava", "amount": result.get("amount"), "currency": result.get("currency"),
            "amount_rubles": rubles, "generations": generations, "contract_id": contract_id,
            "source": source,
        })


async def _create_and_send_stars_invoice(
    message,
    telegram_id: int,
    generations: int,
    rubles: int,
    analytics: AnalyticsClient | None = None,
    source: str = "menu",
    offer_code: str | None = None,
    replace: bool = False,
    state: FSMContext | None = None,
):
    """Send a native Telegram Stars invoice for the selected package.

    Stars settle through the invoice's own Pay button and the bot's
    ``successful_payment`` update — there is no separate confirmation link.
    """
    payment_context = _stars_payment_context(source, offer_code)
    try:
        result = await backend.create_stars_payment(
            telegram_id=telegram_id,
            generations=generations,
            amount_rubles=rubles,
            context=payment_context,
            offer_code=offer_code,
        )
    except Exception as e:
        logger.error("Ошибка создания Stars-платежа: %s", e)
        if source == "onboarding_bonus":
            await message.answer(BONUS_EXPIRED_TEXT)
            await start_payment_flow(message, telegram_id, analytics=analytics)
            return
        if source == "discount":
            await message.answer(_discount_expired_text(offer_code))
            await start_payment_flow(message, telegram_id, analytics=analytics)
            return
        await message.answer(
            "⚠️ Не удалось создать платёж звёздами. Попробуй позже или оплати картой РФ / СБП."
        )
        return

    stars = int(result["stars"])
    generations = int(result["generations"])
    payload = result["payload"]

    back_button = InlineKeyboardButton(
        text="← Выбрать пакет",
        callback_data="onboarding_pay",
    ) if source == "onboarding" else InlineKeyboardButton(
        text="← К предложению",
        callback_data="onboarding_trial_offer",
    ) if source == "onboarding_trial" else InlineKeyboardButton(
        text="← К предложению",
        callback_data="onboarding_bonus_offer",
    ) if source == "onboarding_bonus" else InlineKeyboardButton(
        text="← Выбрать другой пакет",
        callback_data="back_to_packages",
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить ⭐ {stars}", pay=True)],
        [back_button],
    ])

    # Invoices are always fresh messages, so drop the method-choice screen first.
    if replace:
        try:
            await message.delete()
        except Exception:
            logger.warning("Не удалось удалить экран выбора способа перед Stars-инвойсом", exc_info=True)

    invoice_message = await message.answer_invoice(
        title=result["title"],
        description=result["description"],
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"{generations} энергии", amount=stars)],
        reply_markup=keyboard,
    )

    # Remember the invoice card so the success handler can clear it, the same way
    # the YooKassa card is removed after a verified payment.
    if state is not None:
        try:
            await state.update_data(
                stars_invoice_message_id=invoice_message.message_id,
                stars_invoice_payload=payload,
            )
        except Exception:
            logger.warning("Не удалось запомнить сообщение Stars-инвойса", exc_info=True)

    if analytics:
        await analytics.track("payment_link_sent", user_id=str(telegram_id), properties={
            "method": "stars", "stars": stars, "generations": generations,
            "amount_rubles": rubles, "source": source,
        })


async def _create_and_send_robokassa_payment(
    message,
    telegram_id: int,
    generations: int,
    rubles: int,
    analytics: AnalyticsClient | None = None,
    source: str = "menu",
    offer_code: str | None = None,
    replace: bool = False,
):
    """Create the hosted Robokassa card page; settlement stays server-to-server."""
    original_generations, original_rubles = generations, rubles
    flow = _flow_code(source, offer_code)
    payment_context, resolved_offer_code = PAYMENT_FLOW[flow]
    try:
        result = await backend.create_robokassa_payment(
            telegram_id=telegram_id,
            generations=generations,
            amount_rubles=rubles,
            context=payment_context,
            offer_code=resolved_offer_code,
        )
    except Exception as e:
        logger.error("Ошибка создания платежа Robokassa: %s", e)
        await message.answer("⚠️ Не удалось создать платёж. Попробуй позже.")
        return

    confirmation_url = result.get("confirmation_url")
    inv_id = result.get("inv_id")
    generations = int(result.get("generations", generations))
    rubles = int(result.get("amount_rubles", rubles))
    if not confirmation_url or not inv_id:
        await message.answer("⚠️ Не удалось получить ссылку на оплату.")
        return

    back_button = InlineKeyboardButton(
        text="← К способам оплаты",
        callback_data=f"back_to_methods_{original_generations}_{original_rubles}_{flow}",
    ) if source == "onboarding" else InlineKeyboardButton(
        text="← Выбрать другой пакет",
        callback_data="back_to_packages",
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить зарубежной картой 💳", url=confirmation_url)],
        [back_button],
    ])
    payment_message = await _render_checkout_message(
        message,
        f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\n"
        "Оплата зарубежной картой через Robokassa.\n\n"
        + brand.consent_html(),
        keyboard,
        replace=replace,
        parse_mode="HTML",
    )
    try:
        saved_payment = await backend.save_robokassa_payment_message(
            telegram_id, int(inv_id), payment_message.message_id,
        )
        if saved_payment.get("status") == "succeeded":
            await _delete_payment_card(payment_message, str(inv_id))
    except Exception:
        logger.warning("Не удалось сохранить карточку оплаты Robokassa: %s", inv_id, exc_info=True)

    if analytics:
        await analytics.track("payment_link_sent", user_id=str(telegram_id), properties={
            "amount_rubles": rubles, "generations": generations, "inv_id": inv_id,
            "provider": "robokassa", "source": source,
        })


async def _show_foreign_card_min_notice(
    message,
    generations: int,
    rubles: int,
    *,
    replace: bool = False,
) -> None:
    """Explain why a sub-minimum package cannot be paid with a foreign card."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Выбрать другой пакет", callback_data="back_to_packages")],
    ])
    await _render_checkout_message(
        message,
        f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\n"
        f"К сожалению, оплату зарубежной картой мы можем принять только на сумму "
        f"от {FOREIGN_CARD_MIN_RUBLES} ₽.\n\n"
        "Выбери пакет побольше или оплати картой РФ либо через СБП.",
        keyboard,
        replace=replace,
        parse_mode="HTML",
    )


async def start_payment_flow(message, telegram_id: int, analytics: AnalyticsClient | None = None):
    """Start the payment flow: show tier selection in rubles."""
    try:
        await backend.notify_payment_flow(telegram_id)
    except Exception as e:
        logger.error(f"Ошибка уведомления о начале оплаты: {e}")

    try:
        price_data = await backend.get_price(telegram_id, context="menu")
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        await message.answer("⚠️ Не удалось получить цену. Попробуй позже.")
        return

    tiers = price_data.get("tiers", [])
    if not tiers:
        await message.answer("⚠️ Не удалось получить тарифы. Попробуй позже.")
        return

    offers = []
    tier_buttons = []
    for t in tiers:
        gen = t["generations"]
        rubles = t.get("rubles", 0)
        if not rubles:
            continue
        offers.append(_format_tier_offer(t))
        bonus = int(t.get("bonus_generations") or 0)
        tier_buttons.append(InlineKeyboardButton(
            text=f"{gen} + {bonus} 🎁 · {rubles} ₽" if bonus else f"{gen} · {rubles} ₽",
            callback_data=f"buy_tier_{gen}_{rubles}",
        ))

    if not tier_buttons:
        await message.answer("⚠️ Нет доступных тарифов. Попробуй позже.")
        return

    buttons = [tier_buttons[index:index + 2] for index in range(0, len(tier_buttons), 2)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    offers_text = "\n\n".join(offers)
    friend_note = friend_bonus_note(price_data.get("referral_friend_bonus"))
    if friend_note:
        offers_text = f"{offers_text}\n\n{friend_note}"
    await message.answer(
        "Выбирай свой пакет и создавай больше классных фото ✨\n\n"
        f"{offers_text}\n\n"
        "Выбирай пакет ниже 👇",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    if analytics:
        await analytics.track("paywall_shown", user_id=str(telegram_id), properties={"source": "menu", "tiers_count": len(tier_buttons)})


async def start_onboarding_payment(
    message,
    telegram_id: int,
    state: FSMContext,
    analytics: AnalyticsClient | None = None,
    *,
    replace: bool = False,
):
    """Show the ordinary first-purchase package."""
    # The backend intentionally returns a regular menu tier for users who have
    # already paid. Do not pass that tier back with the onboarding context: it
    # is rejected by the server and used to produce a misleading generic error.
    try:
        user_data = await backend.get_user(telegram_id)
        if user_data.get("user", {}).get("has_purchased"):
            await state.clear()
            await message.answer(
                "У тебя уже есть энергия ⚡ — выбери, что хочешь создать 👇",
                reply_markup=get_main_menu_keyboard(telegram_id),
            )
            return
    except Exception as e:
        logger.warning("Не удалось проверить статус покупки перед онбординг-оплатой: %s", e)

    try:
        price_data = await backend.get_price(telegram_id, context="onboarding_paywall")
        tiers = price_data.get("tiers") or []
        if len(tiers) != 1:
            raise ValueError("Некорректный ответ с пакетом первой покупки")
        tier = tiers[0]
        generations, rubles = int(tier["generations"]), int(tier["rubles"])
    except Exception as e:
        logger.error(f"Ошибка получения цены онбординга: {e}")
        await message.answer("⚠️ Не удалось получить тариф. Попробуй позже.")
        return

    if not rubles:
        await message.answer("⚠️ Пакеты недоступны.")
        return

    await state.set_state(PhotoUploadStates.onboarding_paywall)
    await _show_payment_method_choice(
        message, telegram_id=telegram_id, generations=generations, rubles=rubles,
        source="onboarding", replace=replace, analytics=analytics,
    )


async def start_onboarding_bonus_offer(
    message,
    telegram_id: int,
    state: FSMContext,
    analytics: AnalyticsClient | None = None,
    *,
    replace: bool = False,
):
    """Show the delayed first-purchase +50% offer (2h..26h after the paywall).

    The backend owns the window: outside it the price call returns plain
    packages, and this falls back to the ordinary first-purchase screen.
    """
    try:
        price_data = await backend.get_price(telegram_id, context="onboarding_bonus")
        tiers = [
            {
                "generations": int(tier["generations"]),
                "rubles": int(tier["rubles"]),
                "bonus_generations": int(tier.get("bonus_generations") or 0),
            }
            for tier in price_data.get("tiers") or []
        ]
    except Exception as e:
        logger.error("Ошибка получения бонусного предложения: %s", e)
        tiers = []
        price_data = {}

    if not tiers or not all(tier["bonus_generations"] and tier["rubles"] for tier in tiers):
        await start_onboarding_payment(message, telegram_id, state, analytics, replace=replace)
        return

    await state.set_state(PhotoUploadStates.onboarding_paywall)
    offer_text = _onboarding_bonus_offer_text(tiers)
    friend_note = friend_bonus_note(price_data.get("referral_friend_bonus"))
    if friend_note:
        offer_text = f"{offer_text}\n\n{friend_note}"
    await _render_checkout_message(
        message,
        offer_text,
        _onboarding_bonus_offer_keyboard(tiers, telegram_id),
        replace=replace,
        parse_mode="HTML",
    )
    if analytics:
        await analytics.track(
            "onboarding_bonus_offer_shown",
            user_id=str(telegram_id),
            properties={"tiers_count": len(tiers)},
        )


async def start_gift_bonus_tier(
    message,
    telegram_id: int,
    total_energy: int,
    state: FSMContext,
    analytics: AnalyticsClient | None = None,
):
    """Open checkout of the +50% package the gift picture's button named.

    ``total_energy`` is the figure on the button (paid + bonus, e.g. 15 = 10 + 5).
    When the bonus window is closed or the package is gone, show the regular
    offer instead of a dead button.
    """
    try:
        price_data = await backend.get_price(telegram_id, context="onboarding_bonus")
        tier = next(
            (
                item for item in price_data.get("tiers") or []
                if int(item.get("bonus_generations") or 0)
                and int(item["generations"]) + int(item["bonus_generations"]) == total_energy
            ),
            None,
        )
    except Exception as e:
        logger.error("Ошибка получения бонусного пакета подарка: %s", e)
        tier = None
    if tier is None:
        await start_onboarding_bonus_offer(message, telegram_id, state, analytics)
        return
    await state.set_state(PhotoUploadStates.onboarding_paywall)
    await _show_payment_method_choice(
        message,
        telegram_id=telegram_id,
        generations=int(tier["generations"]),
        rubles=int(tier["rubles"]),
        source="onboarding_bonus",
        analytics=analytics,
    )


async def start_discount_tier(
    message,
    telegram_id: int,
    slug: str,
    generations: int | None,
    state: FSMContext,
    analytics: AnalyticsClient | None = None,
):
    """Open checkout of the package a discount push button named (``p_<slug>-<generations>``).

    The backend owns the offer: it answers with the discounted tiers only while the push is live
    for this user (``offer_slug`` echoes the slug). Anything else (expired, used, not this user's)
    says so and shows the ordinary packages instead of a dead button.
    """
    try:
        price_data = await backend.get_price(telegram_id, context=f"discount:{slug}")
        live = price_data.get("offer_slug") == slug
        tiers = [
            {"generations": int(tier["generations"]), "rubles": int(tier["rubles"])}
            for tier in price_data.get("tiers") or []
        ]
    except Exception as e:
        logger.error("Ошибка получения скидочного предложения %s: %s", slug, e)
        live, tiers = False, []
    tier = next((item for item in tiers if item["generations"] == generations), None)
    if not live or not tiers or (tier is None and generations is not None):
        await message.answer(_discount_expired_text(slug))
        await start_payment_flow(message, telegram_id, analytics=analytics)
        return
    if tier is None:  # a link without a package: the offer has one package or the first one is shown
        tier = tiers[0]
    if slug != WINBACK_SLUG:  # a returning buyer is not in the onboarding funnel
        await state.set_state(PhotoUploadStates.onboarding_paywall)
    await _show_payment_method_choice(
        message,
        telegram_id=telegram_id,
        generations=tier["generations"],
        rubles=tier["rubles"],
        source="discount",
        offer_code=slug,
        analytics=analytics,
    )


@router.callback_query(lambda cb: cb.data and cb.data.startswith("buy_onboarding_bonus_tier_"))
async def handle_buy_onboarding_bonus_tier(callback: CallbackQuery, analytics: AnalyticsClient):
    """Choose a payment method for a +50% first-purchase package."""
    await callback.answer()
    try:
        _, _, _, _, raw_generations, raw_rubles = callback.data.split("_", 5)
        generations, rubles = int(raw_generations), int(raw_rubles)
    except (AttributeError, ValueError):
        await callback.message.answer("⚠️ Ошибка выбора пакета.")
        return
    await _show_payment_method_choice(
        callback.message,
        telegram_id=callback.from_user.id,
        generations=generations,
        rubles=rubles,
        source="onboarding_bonus",
        replace=True,
        analytics=analytics,
    )


@router.callback_query(lambda cb: cb.data == "onboarding_bonus_offer")
async def handle_onboarding_bonus_offer(callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient):
    """Return from a bonus checkout to the +50% offer."""
    await callback.answer()
    await start_onboarding_bonus_offer(
        callback.message, callback.from_user.id, state, analytics, replace=True,
    )


async def start_onboarding_trial_payment(
    message,
    telegram_id: int,
    state: FSMContext,
    analytics: AnalyticsClient | None = None,
    *,
    replace: bool = False,
):
    """Show the base 10-generation first offer before bonus reminders begin."""
    try:
        price_data = await backend.get_price(telegram_id, context="onboarding_paywall")
        tiers = price_data.get("tiers") or []
        tier = next((item for item in tiers if int(item.get("generations") or 0) == 10), None)
        if tier is None or not int(tier.get("rubles") or 0):
            raise ValueError("10-generation onboarding trial package is unavailable")
        generations, rubles = int(tier["generations"]), int(tier["rubles"])
    except Exception as e:
        logger.error("Ошибка получения стартового пакета онбординга: %s", e)
        await message.answer("⚠️ Не удалось получить предложение. Попробуй позже.")
        return

    await state.set_state(PhotoUploadStates.onboarding_paywall)
    offer_text = _onboarding_trial_offer_text(generations, rubles)
    friend_note = friend_bonus_note(price_data.get("referral_friend_bonus"))
    if friend_note:
        offer_text = f"{offer_text}\n\n{friend_note}"
    await _render_checkout_message(
        message,
        offer_text,
        _onboarding_trial_offer_keyboard(generations, rubles),
        replace=replace,
        parse_mode="HTML",
    )
    if analytics:
        await analytics.track(
            "onboarding_trial_offer_shown",
            user_id=str(telegram_id),
            properties={"generations": generations, "rubles": rubles},
        )


@router.callback_query(lambda cb: cb.data and cb.data.startswith("buy_onboarding_tier_"))
async def handle_buy_onboarding_tier(callback: CallbackQuery, analytics: AnalyticsClient):
    """Choose a payment method for a selected first-purchase package."""
    await callback.answer()
    try:
        _, _, _, raw_generations, raw_rubles = callback.data.split("_", 4)
        generations, rubles = int(raw_generations), int(raw_rubles)
    except (AttributeError, ValueError):
        await callback.message.answer("⚠️ Ошибка выбора пакета.")
        return
    await _show_payment_method_choice(
        callback.message,
        telegram_id=callback.from_user.id,
        generations=generations,
        rubles=rubles,
        source="onboarding",
        replace=True,
        analytics=analytics,
    )


@router.callback_query(lambda cb: cb.data and cb.data.startswith("buy_onboarding_trial_tier_"))
async def handle_buy_onboarding_trial_tier(callback: CallbackQuery, analytics: AnalyticsClient):
    """Open payment for the initial 10-generation offer, without a bonus."""
    await callback.answer()
    try:
        _, _, _, _, raw_generations, raw_rubles = callback.data.split("_", 5)
        generations, rubles = int(raw_generations), int(raw_rubles)
    except (AttributeError, ValueError):
        await callback.message.answer("⚠️ Ошибка выбора пакета.")
        return
    if generations != 10:
        await callback.message.answer("⚠️ Этот пакет недоступен.")
        return
    await _show_payment_method_choice(
        callback.message,
        telegram_id=callback.from_user.id,
        generations=generations,
        rubles=rubles,
        source="onboarding_trial",
        replace=True,
        analytics=analytics,
    )


@router.callback_query(lambda cb: cb.data == "onboarding_trial_offer")
async def handle_onboarding_trial_offer(callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient):
    """Return from the initial payment card to the initial non-bonus offer."""
    await callback.answer()
    await start_onboarding_trial_payment(
        callback.message,
        callback.from_user.id,
        state,
        analytics,
        replace=True,
    )


@router.callback_query(lambda cb: cb.data == "buy_generations")
async def handle_buy_generations(callback: CallbackQuery, analytics: AnalyticsClient):
    """User clicked buy button — show tier selection in rubles."""
    await callback.answer()
    await start_payment_flow(callback.message, callback.from_user.id, analytics=analytics)


@router.callback_query(lambda cb: cb.data and cb.data.startswith("buy_tier_"))
async def handle_buy_tier(callback: CallbackQuery, analytics: AnalyticsClient):
    """User selected a tier — choose a provider by issuing country of the card."""
    await callback.answer()

    try:
        parts = callback.data.split("_")
        generations = int(parts[2])
        rubles = int(parts[3])
    except (IndexError, ValueError):
        await callback.message.answer("⚠️ Ошибка выбора тарифа.")
        return

    await _show_payment_method_choice(
        callback.message, telegram_id=callback.from_user.id,
        generations=generations, rubles=rubles, source="menu_tier", replace=True,
        analytics=analytics,
    )


@router.callback_query(lambda cb: cb.data and cb.data.startswith("pay_"))
async def handle_payment_provider(
    callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient,
):
    """Create only the selected provider's payment intent."""
    await callback.answer()
    try:
        _, provider, raw_generations, raw_rubles, flow = callback.data.split("_", 4)
        if flow in {"r1", "r2", "r3", "fp"}:
            await start_payment_flow(callback.message, callback.from_user.id, analytics=analytics)
            return
        generations, rubles = int(raw_generations), int(raw_rubles)
        context, offer_code = PAYMENT_FLOW[flow]
    except (AttributeError, KeyError, ValueError):
        await callback.message.answer("⚠️ Ошибка выбора способа оплаты.")
        return

    source = {
        "onboarding_paywall": "onboarding",
        "onboarding_trial": "onboarding_trial",
        "onboarding_bonus": "onboarding_bonus",
        "menu": "menu",
    }.get(context, flow)
    if context.startswith("discount:"):
        source = "discount"
        if provider in ("l", "r"):  # a discount is not sold through the foreign-card providers
            provider = "y"
    if provider == "l" and not config.LAVA_PAYMENTS_ENABLED:
        # A lava.top button left in an old message after lava.top was
        # switched off: open the RU card / SBP checkout for the same package.
        await _create_and_send_payment(
            callback.message, callback.from_user.id, generations, rubles, analytics,
            source=source, offer_code=offer_code, replace=True,
        )
    elif provider == "l":
        await _create_and_send_lava_payment(
            callback.message, callback.from_user.id, generations, rubles, analytics,
            source=source, offer_code=offer_code, replace=True, state=state,
        )
    elif provider == "s" and not config.STARS_PAYMENTS_ENABLED:
        # A Stars button left in an old message after Stars were switched
        # off: show the current payment methods for the same package.
        await _show_payment_method_choice(
            callback.message, telegram_id=callback.from_user.id, generations=generations,
            rubles=rubles, source=source, offer_code=offer_code, replace=True, analytics=analytics,
        )
    elif provider == "s":
        await _create_and_send_stars_invoice(
            callback.message, callback.from_user.id, generations, rubles, analytics,
            source=source, offer_code=offer_code, replace=True, state=state,
        )
    elif provider in ("y", "r"):
        # "r" only reaches here from a stale foreign-card button in an old
        # message; the checkout is retired, so fall back to the RU card / SBP
        # provider instead of a dead end.
        await _create_and_send_payment(
            callback.message, callback.from_user.id, generations, rubles, analytics,
            source=source, offer_code=offer_code, replace=True,
        )
    else:
        await callback.message.answer("⚠️ Неизвестный способ оплаты.")


@router.callback_query(lambda cb: cb.data == "back_to_packages")
async def handle_back_to_packages(callback: CallbackQuery, analytics: AnalyticsClient):
    """Discard the payment card before returning to the current price list.

    A created provider link can still be completed later. Deleting this message
    first ensures its eventual automatic cleanup never removes a newer checkout.
    """
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        logger.warning("Не удалось удалить карточку оплаты перед возвратом к пакетам", exc_info=True)
    await start_payment_flow(callback.message, callback.from_user.id, analytics)


@router.callback_query(lambda cb: cb.data and cb.data.startswith("back_to_methods_"))
async def handle_back_to_payment_methods(callback: CallbackQuery, analytics: AnalyticsClient):
    """Return an onboarding checkout to its original discounted offer."""
    try:
        _, _, _, raw_generations, raw_rubles, flow = callback.data.split("_", 5)
        generations, rubles = int(raw_generations), int(raw_rubles)
        context, offer_code = PAYMENT_FLOW[flow]
        if context != "onboarding_paywall":
            raise ValueError("Unsupported payment flow")
    except (AttributeError, KeyError, ValueError):
        await callback.answer()
        await callback.message.answer("⚠️ Не удалось вернуть способы оплаты. Попробуй ещё раз.")
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        logger.warning("Не удалось удалить карточку оплаты перед возвратом к способам", exc_info=True)
    await _show_payment_method_choice(
        callback.message,
        telegram_id=callback.from_user.id,
        generations=generations,
        rubles=rubles,
        source="onboarding",
        offer_code=offer_code,
        analytics=analytics,
    )


@router.callback_query(lambda cb: cb.data and cb.data.startswith("check_yookassa_"))
async def handle_check_yookassa(callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient):
    """User clicked 'Check payment' — verify YooKassa payment status."""
    yookassa_id = callback.data.replace("check_yookassa_", "")
    telegram_id = callback.from_user.id

    try:
        result = await backend.check_yookassa_payment(telegram_id, yookassa_id)
    except Exception as e:
        logger.error(f"Ошибка проверки платежа ЮКасса: {e}")
        await callback.answer()
        await callback.message.answer("⚠️ Не удалось проверить статус. Попробуй ещё раз.")
        return

    if not result.get("found"):
        await callback.answer()
        await callback.message.answer("⚠️ Платёж не найден.")
        return

    status = result.get("status")
    generations = result.get("generations", 0)

    if status == "succeeded":
        await callback.answer("Оплата подтверждена ✅")
        current_state = await state.get_state()
        if current_state == PhotoUploadStates.onboarding_paywall.state:
            await state.clear()

        try:
            user_data = await backend.get_user(telegram_id)
            remaining = user_data.get("generations_remaining", "?")
        except Exception:
            remaining = "?"

        await _delete_payment_card(callback.message, yookassa_id)
        await callback.message.answer(
            f"✅ Оплата прошла!\n\n"
            f"Тебе доступно {remaining} ⚡ энергии. Создавай новые образы, изменяй фото, "
            f"улучшай фото{' или оживляй снимки' if brand.VIDEO_FEATURES_ENABLED else ''} 👇",
            reply_markup=get_main_menu_keyboard(callback.from_user.id),
        )

        if analytics:
            await analytics.track("payment_confirmed", user_id=str(telegram_id), properties={
                "yookassa_id": yookassa_id, "generations": generations,
            })

    elif status == "canceled":
        await callback.answer()
        await callback.message.edit_text("❌ Платёж отменён. Пакет не списан — попробуй ещё раз из меню.")

    else:
        await callback.answer("Платёж ещё обрабатывается. Подожди минуту и проверь снова.", show_alert=True)


@router.callback_query(lambda cb: cb.data and cb.data.startswith("check_robokassa_"))
async def handle_check_robokassa(callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient):
    """Check whether the signed ResultURL callback already settled the invoice."""
    try:
        inv_id = int(callback.data.replace("check_robokassa_", ""))
        result = await backend.check_robokassa_payment(callback.from_user.id, inv_id)
    except Exception as e:
        logger.error("Ошибка проверки платежа Robokassa: %s", e)
        await callback.answer()
        await callback.message.answer("⚠️ Не удалось проверить статус. Попробуй ещё раз.")
        return

    if not result.get("found"):
        await callback.answer()
        await callback.message.answer("⚠️ Платёж не найден.")
        return
    if result.get("status") == "succeeded":
        await callback.answer("Оплата подтверждена ✅")
        if await state.get_state() == PhotoUploadStates.onboarding_paywall.state:
            await state.clear()
        await _delete_payment_card(callback.message, str(inv_id))
        try:
            user_data = await backend.get_user(callback.from_user.id)
            remaining = user_data.get("generations_remaining", "?")
        except Exception:
            remaining = "?"
        await callback.message.answer(
            f"✅ Оплата прошла!\n\nТебе доступно {remaining} ⚡ энергии. Выбирай, что создать 👇",
            reply_markup=get_main_menu_keyboard(callback.from_user.id),
        )
        if analytics:
            await analytics.track("payment_confirmed", user_id=str(callback.from_user.id), properties={
                "inv_id": inv_id, "generations": result.get("generations", 0), "provider": "robokassa",
            })
        return

    await callback.answer("Платёж ещё обрабатывается. Подожди минуту и проверь снова.", show_alert=True)


@router.callback_query(lambda cb: cb.data and cb.data.startswith("check_lava_"))
async def handle_check_lava(callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient):
    """«Я оплатил»: settle from lava.top if its webhook has not arrived yet."""
    contract_id = callback.data.replace("check_lava_", "")
    try:
        result = await backend.check_lava_payment(callback.from_user.id, contract_id)
    except Exception as e:
        logger.error("Ошибка проверки платежа lava.top: %s", e)
        await callback.answer()
        await callback.message.answer("⚠️ Не удалось проверить статус. Попробуй ещё раз.")
        return

    if not result.get("found"):
        await callback.answer()
        await callback.message.answer("⚠️ Платёж не найден.")
        return
    if result.get("status") == "succeeded":
        await callback.answer("Оплата подтверждена ✅")
        if await state.get_state() == PhotoUploadStates.onboarding_paywall.state:
            await state.clear()
        # The backend success push already removed the card and announced the
        # balance; this only covers a card that is still on screen.
        await _delete_payment_card(callback.message, contract_id)
        if analytics:
            await analytics.track("payment_confirmed", user_id=str(callback.from_user.id), properties={
                "contract_id": contract_id, "generations": result.get("generations", 0), "provider": "lava",
            })
        return

    await callback.answer("Платёж ещё обрабатывается. Подожди минуту и проверь снова.", show_alert=True)


@router.message(StateFilter(LavaCheckoutStates.waiting_for_email), F.text)
async def handle_lava_email(message, state: FSMContext, analytics: AnalyticsClient):
    """The buyer typed the e-mail for the lava.top receipt: continue to the pay link."""
    email = (message.text or "").strip()
    data = await state.get_data()
    checkout = data.get("lava_checkout")
    if not checkout:
        await state.set_state(data.get("lava_previous_state") or None)
        await start_payment_flow(message, message.from_user.id, analytics=analytics)
        return
    if not LAVA_EMAIL_RE.match(email) or len(email) > 254:
        await _ask_lava_email(message, state, checkout=checkout, retry=True)
        return

    await _restore_state_after_lava_email(state)
    if not config.LAVA_PAYMENTS_ENABLED:
        # lava.top was switched off while this buyer was typing the e-mail.
        await _create_and_send_payment(
            message, message.from_user.id, int(checkout["generations"]), int(checkout["rubles"]), analytics,
            source=checkout["source"], offer_code=checkout.get("offer_code"),
        )
        return
    if analytics:
        await analytics.track("lava_email_entered", user_id=str(message.from_user.id), properties={
            "generations": checkout["generations"], "source": checkout["source"],
        })
    await _create_and_send_lava_payment(
        message, message.from_user.id, int(checkout["generations"]), int(checkout["rubles"]), analytics,
        source=checkout["source"], offer_code=checkout.get("offer_code"), state=state, email=email,
    )


@router.callback_query(lambda cb: cb.data == "lava_email_cancel")
async def handle_lava_email_cancel(callback: CallbackQuery, state: FSMContext, analytics: AnalyticsClient):
    """Leave the e-mail step and show the payment methods for the same package again."""
    await callback.answer()
    checkout = await _restore_state_after_lava_email(state)
    if not checkout:
        await start_payment_flow(callback.message, callback.from_user.id, analytics=analytics)
        return
    await _show_payment_method_choice(
        callback.message,
        telegram_id=callback.from_user.id,
        generations=int(checkout["generations"]),
        rubles=int(checkout["rubles"]),
        source=checkout["source"],
        offer_code=checkout.get("offer_code"),
        replace=True,
        analytics=analytics,
    )
