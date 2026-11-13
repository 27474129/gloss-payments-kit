"""Shared external-payment flow for Telegram and MAX."""
from __future__ import annotations

from typing import Protocol

from core.ctx import BotContext
from core.state import StateStore
from core.texts.ru import (
    ONBOARDING_DONE_FALLBACK,
    PAY_CHECK_BUTTON,
    PAY_OPEN_LINK,
    PAY_PENDING,
    onboarding_offer,
    pay_button,
    pay_success,
)
from core.ui import Button, Keyboard


class PaymentBackend(Protocol):
    async def paywall_shown(self, user_id: str) -> None: ...
    async def get_price(self, user_id: str) -> dict: ...
    async def create_payment(self, user_id: str, generations: int, amount_rubles: int) -> dict: ...
    async def payment_status(self, user_id: str, payment_id: str) -> dict: ...
    async def get_me(self, user_id: str) -> dict: ...


def paywall_keyboard(generations: int, rubles: int) -> Keyboard:
    return Keyboard.from_rows([
        [Button.callback(pay_button(generations, rubles), "pay")],
    ])


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
def payment_link_keyboard(url: str, pay_buttons: list[tuple[str, str]] | None = None) -> Keyboard:
    link_rows = [
        [Button.link(text, link) for text, link in row] for row in pay_button_rows(pay_buttons or [])
    ] or [[Button.link("Оплатить", url)]]
    return Keyboard.from_rows([
        *link_rows,
        [Button.callback(PAY_CHECK_BUTTON, "pay_check")],
    ])
# --- /T-BANK ---


async def show_paywall(context: BotContext, state: StateStore, backend: PaymentBackend) -> None:
    try:
        await backend.paywall_shown(context.user.external_id)
        price = await backend.get_price(context.user.external_id)
        generations, rubles = int(price["generations"]), int(price["rubles"])
    except Exception:
        await state.update(context.user, step="paywall")
        await context.send_text(ONBOARDING_DONE_FALLBACK)
        return
    await state.update(
        context.user, step="paywall", offer={"generations": generations, "rubles": rubles},
    )
    await context.send_text(
        onboarding_offer(generations, rubles),
        keyboard=paywall_keyboard(generations, rubles),
    )


# --- T-BANK --- changed: handle_callback
async def handle_callback(
    context: BotContext, state: StateStore, backend: PaymentBackend, payload: str,
    *, success_keyboard: Keyboard | None = None,
) -> bool:
    current = dict(await state.get(context.user))
    if payload == "pay":
        offer = current.get("offer") or {}
        try:
            if not offer:
                price = await backend.get_price(context.user.external_id)
                offer = {"generations": int(price["generations"]), "rubles": int(price["rubles"])}
            payment = await backend.create_payment(
                context.user.external_id, int(offer["generations"]), int(offer["rubles"]),
            )
        except Exception:
            await context.answer_callback("Не получилось создать платёж, попробуй ещё раз")
            return True
        await state.update(context.user, payment_id=str(payment["yookassa_id"]))
        await context.answer_callback("Ссылка на оплату ниже")
        await context.send_text(
            PAY_OPEN_LINK,
            keyboard=payment_link_keyboard(payment["confirmation_url"], checkout_pay_buttons(payment)),
        )
        return True
    if payload != "pay_check":
        return False
    payment_id = current.get("payment_id")
    if not payment_id:
        await context.answer_callback("Нет активного платежа")
        return True
    try:
        status = await backend.payment_status(context.user.external_id, str(payment_id))
    except Exception:
        await context.answer_callback("Не удалось проверить, попробуй позже")
        return True
    if not status.get("paid"):
        await context.answer_callback("Оплата ещё не прошла")
        return True
    balance = status.get("new_balance")
    if balance is None:
        balance = (await backend.get_me(context.user.external_id)).get("generations_remaining", 0)
    await state.update(context.user, step="done", payment_id=None)
    await context.answer_callback("Оплата получена ✅")
    await context.send_text(pay_success(int(balance)), keyboard=success_keyboard)
    return True
# --- /T-BANK ---
