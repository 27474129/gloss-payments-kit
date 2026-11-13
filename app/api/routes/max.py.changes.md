# app/api/routes/max.py — что здесь наше

Файл `app/api/routes/max.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `yookassa_create` — создание платежа в боте MAX: выбор провайдера, `payment_gateway.create_payment`, `provider`/`provider_order_id` в записи (в обеих ветках: с `operation_key` и без), `pay_buttons` в ответе | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 2 | `yookassa_status` — опрос статуса в боте MAX: `payment_gateway.get_payment_status(row)` вместо вызова ЮKassa | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |

**Уровень модуля** (импорты и константы — добавь недостающее):

* import: bot_pay, payment_gateway, tbank_acq

## Чего не хватает у вас (проверено по снимку вашего репозитория от 09.11 — перепроверь)

* `yookassa_create` использует то, чего нет в вашем файле/дереве: `_offer_slug`

Модули кита (`tbank_acq`, `payment_gateway`, `bot_pay`, `pay_ticket`, `tbank_methods`, `miniapp_pay`) лежат рядом — копируй их первыми. Остальное — это модули/функции нашего форка, которых у вас нет: **не переноси** вызовы, которые к Т-Банку не относятся (рефералка, скидочная лесенка, Метрика, пиксель VK и т. п.), а недостающее по делу замени своим аналогом.

## Важные оговорки

* Т-Банк здесь только в `yookassa_create` (создание платежа в боте MAX) и в статусе (`yookassa_status`, если он в изменённых блоках выше). Всё остальное в файле — наш MAX-мост, он вам не нужен.
* Повторный вызов с тем же `operation_key` должен отдавать тот же платёж и те же кнопки — не ломай ветку `existing`.

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `import: bot_pay, payment_gateway, tbank_acq`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+from app.services import bot_pay, payment_gateway, tbank_acq
```

### `yookassa_create`

```diff
--- yookassa_create (до Т-Банка)
+++ yookassa_create (с Т-Банком)
@@ -3,8 +3,11 @@
     body: MaxYookassaCreateRequest, pool: asyncpg.Pool = Depends(get_db),
 ):
-    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
+    if not payment_gateway.yookassa_configured() and not tbank_acq.is_configured():
         raise HTTPException(status_code=503, detail="YooKassa not configured")
 
     user = (await _resolve_user(pool, body.external_id))["user"]
+    provider = payment_gateway.choose_provider(user["id"])
+    if not payment_gateway.configured(provider):
+        raise HTTPException(status_code=503, detail="YooKassa not configured")
 
     if not await _valid_max_payment_tier(
@@ -34,5 +37,5 @@
                 existing = await conn.fetchrow(
                     """
-                    SELECT yookassa_id, status, confirmation_url, amount_rubles, generations, bonus_generations
+                    SELECT yookassa_id, status, confirmation_url, amount_rubles, generations, bonus_generations, provider
                     FROM yookassa_payments
                     WHERE user_id = $1
@@ -53,5 +56,6 @@
                     raise HTTPException(status_code=409, detail="Payment attempt is no longer active")
                 else:
-                    result = await yookassa_service.create_sbp_payment(
+                    result = await payment_gateway.create_payment(
+                        provider,
                         amount_rubles=body.amount_rubles,
                         description=f"{settings.BRAND_NAME} — пакет {body.generations} энергии",
@@ -59,4 +63,5 @@
                         user_id=user["id"],
                         return_url=f"https://max.ru/{settings.MAX_BOT_USERNAME}",
+                        device_data=bot_pay.BOT_DEVICE_DATA if provider == payment_gateway.TBANK and settings.TBANK_BUTTONS else None,
                     )
                     await yookassa_repo.create_payment_on_connection(
@@ -67,8 +72,11 @@
                         offer_slug=_offer_slug(body.context),
                         bonus_generations=bonus_generations,
+                        provider=result.get("provider", "yookassa"),
+                        provider_order_id=result.get("order_id"),
                     )
                     created = True
     else:
-        result = await yookassa_service.create_sbp_payment(
+        result = await payment_gateway.create_payment(
+            provider,
             amount_rubles=body.amount_rubles,
             description=f"{settings.BRAND_NAME} — пакет {body.generations} энергии",
@@ -76,4 +84,5 @@
             user_id=user["id"],
             return_url=f"https://max.ru/{settings.MAX_BOT_USERNAME}",
+            device_data=bot_pay.BOT_DEVICE_DATA if provider == payment_gateway.TBANK and settings.TBANK_BUTTONS else None,
         )
         await yookassa_repo.create_payment(
@@ -84,4 +93,6 @@
             offer_slug=_offer_slug(body.context),
             bonus_generations=bonus_generations,
+            provider=result.get("provider", "yookassa"),
+            provider_order_id=result.get("order_id"),
         )
         created = True
@@ -94,5 +105,6 @@
                         "bonus_generations": bonus_generations,
                         "yookassa_id": result["yookassa_id"], "platform": PLATFORM,
-                        "context": body.context, "offer_slug": _offer_slug(body.context)},
+                        "context": body.context, "offer_slug": _offer_slug(body.context),
+                        "provider": result.get("provider", "yookassa")},
         )
     return {
@@ -102,3 +114,7 @@
         "amount_rubles": body.amount_rubles,
         "bonus_generations": int(result.get("bonus_generations", bonus_generations) or 0),
+        # T-Bank with the buttons on: see the TG create call (a repeated call of the same operation gets the same buttons).
+        **await bot_pay.fields_for_bot(
+            provider=result.get("provider"), payment_id=result["yookassa_id"], user_id=user["id"], platform="max",
+        ),
     }
```

### `yookassa_status`

```diff
--- yookassa_status (до Т-Банка)
+++ yookassa_status (с Т-Банком)
@@ -17,6 +17,6 @@
         raise HTTPException(status_code=404, detail="Payment not found")
 
-    remote = await yookassa_service.get_payment_status(yookassa_id)
-    if remote.get("status") == "succeeded" and yookassa_service.matches_local_payment(
+    remote = await payment_gateway.get_payment_status(local)
+    if remote.get("status") == "succeeded" and payment_gateway.matches_local_payment(
         remote, local,
     ):
```
