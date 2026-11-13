"""T-Bank payment: SberPay · SBP · T-Pay · card as link buttons in the chat (the backend sends ``pay_buttons``)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.handlers.payment import _create_and_send_payment
from core.flows.payment import checkout_pay_buttons, handle_callback, payment_link_keyboard
from core.identity import UserRef
from core.ui import ButtonKind

BASE = "https://app.your-domain.example/api/v1/miniapp/pay-bot?k=gb1.x.y"
PAY_BUTTONS = [
    {"method": "sberpay", "text": "SberPay", "url": f"{BASE}&m=sberpay&p=tg"},
    {"method": "sbp", "text": "СБП", "url": f"{BASE}&m=sbp&p=tg"},
    {"method": "tpay", "text": "T-Pay", "url": f"{BASE}&m=tpay&p=tg"},
    {"method": "card", "text": "Картой", "url": f"{BASE}&m=card&p=tg"},
]


def test_the_buttons_keep_the_order_of_the_backend_and_get_a_mark():
    assert checkout_pay_buttons({"pay_buttons": PAY_BUTTONS}) == [
        ("🟢 SberPay", f"{BASE}&m=sberpay&p=tg"),
        ("⚪ СБП", f"{BASE}&m=sbp&p=tg"),
        ("🟡 T-Pay", f"{BASE}&m=tpay&p=tg"),
        ("💳 Картой", f"{BASE}&m=card&p=tg"),
    ]


def test_no_buttons_or_junk_means_the_single_old_button():
    assert checkout_pay_buttons({}) == [] and checkout_pay_buttons({"pay_buttons": None}) == []
    junk = [None, "x", {"text": "A"}, {"text": "", "url": "https://x/y"}, {"text": "B", "url": "http://x/y"},
            {"text": "C", "url": "javascript:alert(1)"}, {"method": "new", "text": "Новый", "url": "https://x/z"}]
    assert checkout_pay_buttons({"pay_buttons": junk}) == [("Новый", "https://x/z")]  # an unknown way has no mark


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
async def test_the_checkout_card_has_the_four_buttons_two_in_a_row_and_the_back_button(monkeypatch):
    message = _payment_message(monkeypatch, {
        "confirmation_url": "https://app.your-domain.example/api/v1/miniapp/pay-choose?k=gb1.x.y&p=tg",
        "yookassa_id": "771001", "pay_buttons": PAY_BUTTONS,
    })

    await _create_and_send_payment(message, telegram_id=123, generations=10, rubles=299)

    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["🟢 SberPay", "⚪ СБП"], ["🟡 T-Pay", "💳 Картой"], ["← Выбрать другой пакет"],
    ]
    assert [button.url for row in keyboard.inline_keyboard[:2] for button in row] == [b["url"] for b in PAY_BUTTONS]
    assert keyboard.inline_keyboard[2][0].callback_data == "back_to_packages"
    assert "Оплатить" not in str(keyboard)
    assert "ЮKassa" in message.answer.await_args.args[0]  # the text above the buttons is the one it was


@pytest.mark.asyncio
async def test_without_pay_buttons_the_card_is_the_old_single_pay_button(monkeypatch):
    message = _payment_message(monkeypatch, {"confirmation_url": "https://yk/pay", "yookassa_id": "yk-1"})

    await _create_and_send_payment(message, telegram_id=123, generations=10, rubles=299)

    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert [(b.text, b.url, b.callback_data) for row in keyboard.inline_keyboard for b in row] == [
        ("Оплатить 299 ₽", "https://yk/pay", None), ("← Выбрать другой пакет", None, "back_to_packages"),
    ]


def test_the_core_keyboard_has_the_link_buttons_then_the_check_button():
    keyboard = payment_link_keyboard("https://pay.test", checkout_pay_buttons({"pay_buttons": PAY_BUTTONS[:3]}))

    assert [[(b.text, b.kind) for b in row] for row in keyboard.rows[:2]] == [
        [("🟢 SberPay", ButtonKind.LINK), ("⚪ СБП", ButtonKind.LINK)],
        [("🟡 T-Pay", ButtonKind.LINK)],
    ]
    assert [(b.kind, b.value) for b in keyboard.rows[2]] == [(ButtonKind.CALLBACK, "pay_check")]
    old = payment_link_keyboard("https://pay.test")
    assert [(b.text, b.kind, b.value) for b in old.rows[0]] == [("Оплатить", ButtonKind.LINK, "https://pay.test")]


class _State:
    def __init__(self): self.data = {}
    async def get(self, user): return dict(self.data)
    async def update(self, user, **values): self.data.update(values)


class _Context:
    user = UserRef("telegram", "42")
    def __init__(self): self.messages = []
    async def send_text(self, text, *, keyboard=None): self.messages.append((text, keyboard))
    async def answer_callback(self, text=None): pass


def test_the_core_flow_shows_the_buttons_of_the_payment():
    class Backend:
        async def create_payment(self, user_id, generations, amount_rubles):
            return {"yookassa_id": "771001", "confirmation_url": "https://pay.test", "pay_buttons": PAY_BUTTONS}

    async def scenario():
        context, state = _Context(), _State()
        state.data["offer"] = {"generations": 10, "rubles": 299}
        assert await handle_callback(context, state, Backend(), "pay") is True
        keyboard = context.messages[-1][1]
        assert [b.value for row in keyboard.rows[:2] for b in row] == [b["url"] for b in PAY_BUTTONS]
        assert state.data["payment_id"] == "771001"

    asyncio.run(scenario())
