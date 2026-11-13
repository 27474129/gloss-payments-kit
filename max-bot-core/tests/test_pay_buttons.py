"""T-Bank payment: SberPay · SBP · T-Pay · card as link buttons in the MAX chat (the backend sends ``pay_buttons``)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.handlers.payment import _create_and_send_payment
from core.flows.payment import checkout_pay_buttons, handle_callback, payment_link_keyboard
from core.identity import UserRef
from core.state_memory import MemoryStateStore
from core.ui import ButtonKind

BASE = "https://app.your-domain.example/api/v1/miniapp/pay-bot?k=gb1.x.y"
PAY_BUTTONS = [
    {"method": "sberpay", "text": "SberPay", "url": f"{BASE}&m=sberpay&p=max"},
    {"method": "sbp", "text": "СБП", "url": f"{BASE}&m=sbp&p=max"},
    {"method": "tpay", "text": "T-Pay", "url": f"{BASE}&m=tpay&p=max"},
    {"method": "card", "text": "Картой", "url": f"{BASE}&m=card&p=max"},
]
USER = UserRef("max", "17")


def test_the_buttons_keep_the_order_of_the_backend_and_get_a_mark():
    assert checkout_pay_buttons({"pay_buttons": PAY_BUTTONS}) == [
        ("🟢 SberPay", f"{BASE}&m=sberpay&p=max"),
        ("⚪ СБП", f"{BASE}&m=sbp&p=max"),
        ("🟡 T-Pay", f"{BASE}&m=tpay&p=max"),
        ("💳 Картой", f"{BASE}&m=card&p=max"),
    ]


def test_no_buttons_or_junk_means_the_single_old_button():
    assert checkout_pay_buttons({}) == [] and checkout_pay_buttons({"pay_buttons": None}) == []
    junk = [None, "x", {"text": "A"}, {"text": "", "url": "https://x/y"}, {"text": "B", "url": "http://x/y"},
            {"text": "C", "url": "javascript:alert(1)"}, {"method": "new", "text": "Новый", "url": "https://x/z"}]
    assert checkout_pay_buttons({"pay_buttons": junk}) == [("Новый", "https://x/z")]  # an unknown way has no mark


def test_the_core_keyboard_has_the_link_buttons_two_in_a_row_and_keeps_the_back_button():
    keyboard = payment_link_keyboard(
        "https://pay.test", 299, pricing_context="menu", pay_buttons=checkout_pay_buttons({"pay_buttons": PAY_BUTTONS}),
    )

    assert [[(b.text, b.kind) for b in row] for row in keyboard.rows[:2]] == [
        [("🟢 SberPay", ButtonKind.LINK), ("⚪ СБП", ButtonKind.LINK)],
        [("🟡 T-Pay", ButtonKind.LINK), ("💳 Картой", ButtonKind.LINK)],
    ]
    assert [(b.kind, b.value) for b in keyboard.rows[2]] == [(ButtonKind.CALLBACK, "pay.back")] and len(keyboard.rows) == 3
    old = payment_link_keyboard("https://pay.test", 299, pricing_context="menu")
    assert [(b.text, b.kind, b.value) for b in old.rows[0]] == [("Оплатить 299 ₽", ButtonKind.LINK, "https://pay.test")]


class _Context:
    user = USER

    def __init__(self): self.messages = []
    async def send_text(self, text, *, keyboard=None, clear_previous=False): self.messages.append((text, keyboard))
    async def answer_callback(self, text=None): pass


class _Backend:
    def __init__(self, answer): self.answer = answer
    async def create_payment(self, *_args, **_kwargs): return dict(self.answer)


def _tap_pay(answer):
    async def scenario():
        context, state = _Context(), MemoryStateStore()
        await state.update(USER, step="paywall", payment_context="menu", offer={"generations": 10, "rubles": 299})
        assert await handle_callback(context, state, _Backend(answer), "pay") is True
        return context.messages[-1][1], await state.get(USER)

    return asyncio.run(scenario())


def test_the_core_flow_shows_the_buttons_of_the_payment():
    keyboard, state = _tap_pay({"yookassa_id": "771001", "confirmation_url": "https://pay.test", "pay_buttons": PAY_BUTTONS})

    assert [b.value for row in keyboard.rows[:2] for b in row] == [b["url"] for b in PAY_BUTTONS]
    assert state["payment_id"] == "771001"


def test_the_core_flow_without_pay_buttons_keeps_the_single_pay_button():
    keyboard, _ = _tap_pay({"yookassa_id": "yk-1", "confirmation_url": "https://yk/pay"})

    assert [(b.text, b.value) for row in keyboard.rows[:-1] for b in row] == [("Оплатить 299 ₽", "https://yk/pay")]


def _payment_message(monkeypatch, result):
    message = AsyncMock()
    message.answer.return_value = AsyncMock(message_id=987)
    monkeypatch.setattr("app.handlers.payment.backend.create_yookassa_payment", AsyncMock(return_value=result))
    monkeypatch.setattr(
        "app.handlers.payment.backend.save_yookassa_payment_message",
        AsyncMock(return_value={"status": "pending", "payment_message_id": 987}),
    )
    return message


@pytest.mark.asyncio
async def test_the_handler_checkout_card_has_the_four_buttons_and_the_back_button(monkeypatch):
    message = _payment_message(monkeypatch, {
        "confirmation_url": "https://app.your-domain.example/api/v1/miniapp/pay-choose?k=gb1.x.y&p=max",
        "yookassa_id": "771001", "pay_buttons": PAY_BUTTONS,
    })

    await _create_and_send_payment(message, telegram_id=123, generations=10, rubles=299)

    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["🟢 SberPay", "⚪ СБП"], ["🟡 T-Pay", "💳 Картой"], ["← Выбрать другой пакет"],
    ]
    assert [button.url for row in keyboard.inline_keyboard[:2] for button in row] == [b["url"] for b in PAY_BUTTONS]
    assert "Оплатить" not in str(keyboard)


@pytest.mark.asyncio
async def test_the_handler_without_pay_buttons_keeps_the_single_pay_button(monkeypatch):
    message = _payment_message(monkeypatch, {"confirmation_url": "https://yk/pay", "yookassa_id": "yk-1"})

    await _create_and_send_payment(message, telegram_id=123, generations=10, rubles=299)

    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert [(b.text, b.url) for row in keyboard.inline_keyboard[:1] for b in row] == [("Оплатить 💳", "https://yk/pay")]
