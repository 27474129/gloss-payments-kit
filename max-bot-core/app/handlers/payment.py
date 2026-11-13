import logging

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from app.api.backend import backend
from app import config
from core import brand
from app.keyboards.common import get_main_menu_keyboard, get_main_miniapp_button
from core.flows.payment import checkout_pay_buttons, pay_button_rows  # --- T-BANK ---
from app.services.analytics_sdk import AnalyticsClient
from app.states.photo import PhotoUploadStates

logger = logging.getLogger(__name__)
router = Router()
SHOW_FOREIGN_CARD_OPTION = True
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
        try:
            await message.edit_text(text, **kwargs)
            return message
        except Exception:
            logger.warning("Не удалось обновить экран оплаты, создаю новое сообщение", exc_info=True)
    return await message.answer(text, **kwargs)


PAYMENT_FLOW = {
    "m": ("menu", None),
    "o": ("onboarding_paywall", None),
    "r1": ("special_offer", "onboarding_paywall_reminder_1"),
    "r2": ("special_offer", "onboarding_paywall_reminder_2"),
    "r3": ("special_offer", "onboarding_paywall_reminder_3"),
    "fp": ("special_offer", "first_payment_discount"),
}


def _flow_code(source: str, offer_code: str | None) -> str:
    if source == "onboarding":
        return "o"
    if offer_code == "onboarding_paywall_reminder_1":
        return "r1"
    if offer_code == "onboarding_paywall_reminder_2":
        return "r2"
    if offer_code == "onboarding_paywall_reminder_3":
        return "r3"
    if offer_code == "first_payment_discount":
        return "fp"
    return "m"


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
    """Let the customer choose the issuer region before a provider is created."""
    flow = _flow_code(source, offer_code)
    buttons = [
        [InlineKeyboardButton(
            text="🇷🇺 Карта РФ или СБП",
            callback_data=f"pay_y_{generations}_{rubles}_{flow}",
        )],
    ]
    if SHOW_FOREIGN_CARD_OPTION and (
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
    if source == "onboarding":
        buttons.append([get_main_miniapp_button("Посмотреть возможности ✨", telegram_id)])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\nВыбери способ оплаты."
    if source == "onboarding":
        text = (
            f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\n"
            "С ними можно примерять новые образы, менять фото через ИИ, "
            "улучшать фото и оживлять снимки.\n\n"
            "Без подписок и привязки карты: платишь один раз — энергия "
            "остаётся с тобой навсегда.\n\n"
            "Выбери способ оплаты или сначала посмотри, что можно создать."
        )
    if SHOW_FOREIGN_CARD_OPTION and rubles < FOREIGN_CARD_MIN_RUBLES:
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

    price_line = f"{generations} ⚡ энергии — <b>{rubles} ₽</b>"
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
    payment_context = "onboarding_paywall" if source == "onboarding" else "menu"
    if source not in {"menu", "menu_tier", "onboarding"}:
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
        text="← К способам оплаты",
        callback_data=f"back_to_methods_{generations}_{rubles}_{_flow_code(source, offer_code)}",
    ) if source == "onboarding" else InlineKeyboardButton(
        text="← Выбрать другой пакет",
        callback_data="back_to_packages",
    )
    # T-Bank: SberPay · SBP · T-Pay · card instead of the single button (the backend sends ``pay_buttons`` only then).
    pay_rows = [
        [InlineKeyboardButton(text=text, url=link) for text, link in row]
        for row in pay_button_rows(checkout_pay_buttons(result))
    ] or [[InlineKeyboardButton(text="Оплатить 💳", url=confirmation_url)]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[*pay_rows, [back_button]])

    payment_message = await _render_checkout_message(
        message,
        f"<b>{generations} ⚡ энергии · {rubles} ₽</b>\n\n"
        "Оплата российской картой или СБП через ЮKassa.\n"
        "Если платёж не открывается — отключи VPN.\n\n"
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
        tier_buttons.append(InlineKeyboardButton(
            text=f"{gen} · {rubles} ₽",
            callback_data=f"buy_tier_{gen}_{rubles}",
        ))

    if not tier_buttons:
        await message.answer("⚠️ Нет доступных тарифов. Попробуй позже.")
        return

    buttons = [tier_buttons[index:index + 2] for index in range(0, len(tier_buttons), 2)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    offers_text = "\n\n".join(offers)
    await message.answer(
        "Выбирай свой пакет и создавай больше классных фото ✨\n\n"
        f"{offers_text}\n\n"
        "Выбирай пакет ниже 👇",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    if analytics:
        await analytics.track("paywall_shown", user_id=str(telegram_id), properties={"source": "menu", "tiers_count": len(tier_buttons)})


async def start_onboarding_payment(message, telegram_id: int, state: FSMContext, analytics: AnalyticsClient | None = None):
    """Create the single-tier onboarding payment and lock the product menu."""
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
            raise ValueError("Некорректный ответ с онбординг-тарифом")
        tier = tiers[0]
        rubles = tier.get("rubles", 0)
        generations = tier["generations"]
    except Exception as e:
        logger.error(f"Ошибка получения цены онбординга: {e}")
        await message.answer("⚠️ Не удалось получить тариф. Попробуй позже.")
        return

    if not rubles:
        await message.answer("⚠️ Тариф недоступен.")
        return

    await state.set_state(PhotoUploadStates.onboarding_paywall)
    await _show_payment_method_choice(
        message, telegram_id=telegram_id, generations=generations, rubles=rubles, source="onboarding",
        analytics=analytics,
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
async def handle_payment_provider(callback: CallbackQuery, analytics: AnalyticsClient):
    """Create only the selected provider's payment intent."""
    await callback.answer()
    try:
        _, provider, raw_generations, raw_rubles, flow = callback.data.split("_", 4)
        generations, rubles = int(raw_generations), int(raw_rubles)
        context, offer_code = PAYMENT_FLOW[flow]
    except (AttributeError, KeyError, ValueError):
        await callback.message.answer("⚠️ Ошибка выбора способа оплаты.")
        return

    source = "onboarding" if context == "onboarding_paywall" else ("menu" if context == "menu" else flow)
    if provider == "y":
        await _create_and_send_payment(
            callback.message, callback.from_user.id, generations, rubles, analytics,
            source=source, offer_code=offer_code, replace=True,
        )
    elif provider == "r":
        await _create_and_send_robokassa_payment(
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
            f"улучшай фото или оживляй снимки 👇",
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
