# app/api/routes/miniapp_pay.py — файл целиком наш (у вас такого нет)

Весь API оплаты Mini App и страниц возврата: `GET payments/packages`, `POST payments/yookassa/create`, `POST payments/ticket`,
`GET payments/ticket/{ticket}`, `GET payments/{id}`, `POST paywall-opened`, страницы `pay-go`, `pay-return`, `pay-open`, `pay-choose`, `pay-bot`.
Файл не содержит маркеров `# --- T-BANK ---`: он целиком про Т-Банк и оплату в приложении. Он сделан для форка с несколькими модулями,
которых у вас нет, — **вырежи их так** (всё остальное у вас есть, проверено по снимку 09.11):

1. `from app.core import edge` — удали. В `_return_origins()` убери из кортежа `edge.EDGE_MEDIA_URL` (остаются `settings.TBANK_RETURN_URL`, домен вашего Mini App и `settings.MINIAPP_RETURN_ORIGINS`).
2. `from app.db import discount_ladder_repo, pricing_repo, users_repo, yookassa_repo` — убери `discount_ladder_repo`. Строку `from app.services import discount_ladder` удали.
3. Функцию `_live_special_offer(pool, user_id)` (скидочная «лесенка» нашего форка) замени телом `return None`; места вызова не трогай.
4. `payments_routes._offer_slug(context)` (два места: запись платежа и аналитика) — в вашем `payments.py` такой функции нет: добавь `def _offer_slug(context): return None` или подставь `None`.
5. Есть у вас и используются как есть: `payments_routes._validate_payment_amount`, `PaymentCreateRequest`, `_payment_platform`, `_menu_bonus_generations`, `_process_succeeded_payment`; `max_routes._valid_max_payment_tier`, `_max_payment_platform`; `pricing_repo.get_menu_tiers` (список пакетов, тот же, что в меню ботов — если у вас иначе, подставь свой источник цен); `get_miniapp_user`, `is_miniapp_preview` из `app/api/routes/miniapp.py`; `users_repo.get_user_by_id`; `yookassa_repo.get_by_operation_key`; `analytics`.
6. Настройки: `MINIAPP_PAYWALL_INAPP`, `MINIAPP_RETURN_ORIGINS`, `BRAND_NAME` и все `TBANK_*` — в `app/core/config.tbank-block.py.txt`.
7. Подключи роутер в `app/main.py` с префиксом `/api/v1/miniapp` (чек-лист B5).
8. Тест-страховка: `tests/test_miniapp_pay.py`, `tests/test_tbank_buttons.py`, `tests/test_tbank_test_mode.py`, `tests/test_pay_ticket.py`.

Контракты с фронтом, которые нельзя ломать: тип `postMessage` страницы возврата (`app-pay-return`) = `PAY_RETURN_MESSAGE` в `lib/paywall.ts`; формат билета `gp1.…` (подписывается `API_SECRET_TOKEN`); поля ответа `create`: `payment_id`, `confirmation_type`, `confirmation_url`, `provider`, `embed`, `top_level`.
