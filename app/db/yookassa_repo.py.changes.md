# app/db/yookassa_repo.py — что здесь наше

Файл `app/db/yookassa_repo.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `create_payment` — в `INSERT` добавлены `provider`, `provider_order_id` | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 2 | `create_payment_on_connection` — то же для создания внутри транзакции (MAX) | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 3 | `settle_succeeded_payment` — зачисление: статус `test_owner` для тестового терминала (без `has_purchased`, рефералки, Метрики), подпись «T-Bank» в журнале | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 4 | `save_payment_method` — запись способа/карты через `COALESCE` — тип и карта дополняют друг друга в любом порядке | изменена | нет | добавь функцию целиком (рядом с похожими) — в вашем файле её нет вообще: она из нашей доработки до Т-Банка |
| 5 | `reopen_canceled` — платёж Т-Банка, закрытый как неудачный, но оплаченный повторно на той же форме, снова становится `pending` | новая | нет | добавь функцию целиком (рядом с похожими) |
| 6 | `record_refund` — запись возвращённой суммы (`refunded_rubles`, не убывает) | новая | нет | добавь функцию целиком (рядом с похожими) |
| 7 | `list_by_operation_prefix` — платежи одного билета Mini App (`tk:<nonce>:<способ>`) | новая | нет | добавь функцию целиком (рядом с похожими) |

## Чего не хватает у вас (проверено по снимку вашего репозитория от 09.11 — перепроверь)

* `settle_succeeded_payment` использует то, чего нет в вашем файле/дереве: `metrika_purchase_repo` (`app.db.metrika_purchase_repo`)

Модули кита (`tbank_acq`, `payment_gateway`, `bot_pay`, `pay_ticket`, `tbank_methods`, `miniapp_pay`) лежат рядом — копируй их первыми. Остальное — это модули/функции нашего форка, которых у вас нет: **не переноси** вызовы, которые к Т-Банку не относятся (рефералка, скидочная лесенка, Метрика, пиксель VK и т. п.), а недостающее по делу замени своим аналогом.

## Важные оговорки

* В `settle_succeeded_payment` наши правки Т-Банка — ветка `is_test` (тестовый терминал → статус `test_owner`, без `has_purchased`, рефералки, Метрики) и поле `provider`/метка «T-Bank». Вызовы `referrals_repo.apply_settlement`, `metrika_purchase_repo.enqueue_first_purchase`, `vk_pixel_repo.enqueue` — **из нашего форка**, у вас их нет: не переноси их, а свои аналогичные вызовы оберни теми же условиями `if not is_test`.
* `save_payment_method` и колонки `payment_method_type`/`card_*` у вас отсутствуют — сначала миграция `TEMPLATE_yookassa_payment_method.py.txt`, затем функция.
* Колонки `provider`, `provider_order_id`, `refunded_rubles` создаёт `TEMPLATE_tbank_acquiring.py.txt`. Порядок: миграция → код.

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `create_payment`

```diff
--- create_payment (до Т-Банка)
+++ create_payment (с Т-Банком)
@@ -12,5 +12,9 @@
     offer_slug: str | None = None,
     bonus_generations: int = 0,
+    provider: str = "yookassa",
+    provider_order_id: str | None = None,
 ) -> dict:
+    """Store a pending payment. ``yookassa_id`` is the provider's payment id for either provider
+    (T-Bank ``PaymentId`` when ``provider='tbank'``, with its ``OrderId`` in ``provider_order_id``)."""
     async with pool.acquire() as conn:
         row = await conn.fetchrow(
@@ -18,10 +22,10 @@
             INSERT INTO yookassa_payments
                 (user_id, telegram_id, yookassa_id, amount_rubles, generations, status, confirmation_url, platform,
-                 client_operation_key, offer_slug, bonus_generations)
-            VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10)
+                 client_operation_key, offer_slug, bonus_generations, provider, provider_order_id)
+            VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10, $11, $12)
             RETURNING id, yookassa_id, status, confirmation_url, created_at
             """,
             user_id, telegram_id, yookassa_id, amount_rubles, generations, confirmation_url, platform,
-            operation_key, offer_slug, bonus_generations,
+            operation_key, offer_slug, bonus_generations, provider, provider_order_id,
         )
         return dict(row)
```

### `create_payment_on_connection`

```diff
--- create_payment_on_connection (до Т-Банка)
+++ create_payment_on_connection (с Т-Банком)
@@ -12,4 +12,6 @@
     offer_slug: str | None = None,
     bonus_generations: int = 0,
+    provider: str = "yookassa",
+    provider_order_id: str | None = None,
 ) -> dict:
     row = await conn.fetchrow(
@@ -17,10 +19,10 @@
         INSERT INTO yookassa_payments
             (user_id, telegram_id, yookassa_id, amount_rubles, generations, status, confirmation_url, platform,
-             client_operation_key, offer_slug, bonus_generations)
-        VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10)
+             client_operation_key, offer_slug, bonus_generations, provider, provider_order_id)
+        VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10, $11, $12)
         RETURNING id, yookassa_id, status, confirmation_url, created_at
         """,
         user_id, telegram_id, yookassa_id, amount_rubles, generations,
-        confirmation_url, platform, operation_key, offer_slug, bonus_generations,
+        confirmation_url, platform, operation_key, offer_slug, bonus_generations, provider, provider_order_id,
     )
     return dict(row)
```

### `settle_succeeded_payment`

```diff
--- settle_succeeded_payment (до Т-Банка)
+++ settle_succeeded_payment (с Т-Банком)
@@ -1,4 +1,10 @@
 async def settle_succeeded_payment(pool: asyncpg.Pool, yookassa_id: str) -> dict | None:
-    """Atomically mark a payment, credit balance, log it, and mark purchase."""
+    """Atomically mark a payment, credit balance, log it, and mark purchase.
+
+    A payment of the T-Bank TEST terminal (``provider_order_id`` starts with ``gltest``) is credited the same way but
+    stored as ``test_owner`` — the status every report, the NPD receipts and the ad guard already leave out — and
+    touches nothing that counts money or marketing: no ``has_purchased``, no referral reward, no Metrika/VK
+    conversion. The returned row has ``test=True``.
+    """
     async with pool.acquire() as conn:
         async with conn.transaction():
@@ -6,8 +12,10 @@
                 """
                 UPDATE yookassa_payments
-                SET status = 'succeeded', completed_at = NOW()
+                SET status = CASE WHEN provider = 'tbank' AND provider_order_id LIKE 'gltest%' THEN 'test_owner' ELSE 'succeeded' END,
+                    completed_at = NOW()
                 WHERE yookassa_id = $1 AND status = 'pending'
                 RETURNING id, user_id, telegram_id, platform, yookassa_id, amount_rubles, generations, payment_message_id,
-                          offer_slug, bonus_generations AS menu_bonus_generations
+                          offer_slug, bonus_generations AS menu_bonus_generations, provider,
+                          (status = 'test_owner') AS test
                 """,
                 yookassa_id,
@@ -16,5 +24,6 @@
                 return None
 
-            first_purchase = await conn.fetchval(
+            is_test = bool(payment["test"])
+            first_purchase = None if is_test else await conn.fetchval(
                 """
                 UPDATE users
@@ -32,14 +41,18 @@
                 first_purchase=bool(first_purchase),
             )
-            referral = await referrals_repo.apply_settlement(
-                conn, user_id=payment["user_id"], provider="yookassa", payment_id=payment["id"],
-                amount_rubles=payment["amount_rubles"], generations=payment["generations"],
-            )
+            if is_test:
+                referral = {"friend_bonus": 0, "referrer_reward": None}
+            else:
+                referral = await referrals_repo.apply_settlement(
+                    conn, user_id=payment["user_id"], provider="yookassa", payment_id=payment["id"],
+                    amount_rubles=payment["amount_rubles"], generations=payment["generations"],
+                )
             bonus_generations += referral["friend_bonus"]
             credited_generations = payment["generations"] + bonus_generations
-            await metrika_purchase_repo.enqueue_first_purchase(
-                conn, user_id=payment["user_id"], provider="yookassa", provider_payment_id=payment["yookassa_id"],
-                amount_rubles=payment["amount_rubles"], is_first_purchase=bool(first_purchase),
-            )
+            if not is_test:
+                await metrika_purchase_repo.enqueue_first_purchase(
+                    conn, user_id=payment["user_id"], provider="yookassa", provider_payment_id=payment["yookassa_id"],
+                    amount_rubles=payment["amount_rubles"], is_first_purchase=bool(first_purchase),
+                )
             if first_purchase:
                 await vk_pixel_repo.enqueue(
@@ -61,4 +74,5 @@
                 raise RuntimeError(f"Balance not found for payment {yookassa_id}")
 
+            label = "T-Bank TEST" if is_test else "T-Bank" if payment["provider"] == "tbank" else "YooKassa SBP"
             await conn.execute(
                 """
@@ -68,8 +82,8 @@
                 payment["user_id"], credited_generations,
                 (
-                    f"YooKassa SBP: {payment['amount_rubles']}₽"
+                    f"{label}: {payment['amount_rubles']}₽"
                     f" + {bonus_generations} бонусных"
                     if bonus_generations
-                    else f"YooKassa SBP: {payment['amount_rubles']}₽"
+                    else f"{label}: {payment['amount_rubles']}₽"
                 ),
             )
```

### `save_payment_method`

```diff
--- save_payment_method (до Т-Банка)
+++ save_payment_method (с Т-Банком)
@@ -1,8 +1,10 @@
 async def save_payment_method(pool: asyncpg.Pool, yookassa_id: str, method: dict | None) -> None:
-    """Remember how a payment was paid (card first6/last4 and type), for abuse monitoring only.
+    """Remember how a payment was paid (card first6/last4 and type), for abuse monitoring and the ways-to-pay split.
 
+    Every column is only filled while it is empty, so the first answer wins and a later one completes it: T-Bank's
+    notification brings the card (no type), its GetState brings the type (no card), and they may come in either order.
     Best effort: it never raises, so a failure here cannot hold back the settlement.
     """
-    if not method or not method.get("type"):
+    if not method or not (method.get("type") or method.get("card_last4")):
         return
     try:
@@ -10,6 +12,9 @@
             """
             UPDATE yookassa_payments
-               SET payment_method_type = $2, card_first6 = $3, card_last4 = $4, card_type = $5, card_country = $6
-             WHERE yookassa_id = $1 AND payment_method_type IS NULL
+               SET payment_method_type = COALESCE(payment_method_type, $2),
+                   card_first6 = COALESCE(card_first6, $3), card_last4 = COALESCE(card_last4, $4),
+                   card_type = COALESCE(card_type, $5), card_country = COALESCE(card_country, $6)
+             WHERE yookassa_id = $1
+               AND (payment_method_type IS NULL OR (card_last4 IS NULL AND $4::text IS NOT NULL))
             """,
             yookassa_id, method.get("type"), method.get("card_first6"), method.get("card_last4"),
```

### `reopen_canceled`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `record_refund`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `list_by_operation_prefix`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```
