# app/api/routes/payments.py — что здесь наше

Файл `app/api/routes/payments.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `yookassa_create` — создание платежа в боте TG: выбор провайдера (`payment_gateway.choose_provider`), создание через `payment_gateway.create_payment`, поля `provider`/`provider_order_id` в записи, `pay_buttons` в ответе | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 2 | `yookassa_webhook` — вебхук ЮKassa игнорирует платёж чужого провайдера | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 3 | `tbank_notify` — публичная нотификация Т-Банка (`POST /payments/tbank/notify`): подпись, идемпотентность, зачисление тем же `_process_succeeded_payment`, отказ, возврат | новая | нет | добавь функцию целиком (рядом с похожими) |
| 4 | `_save_tbank_method` — после зачисления читает способ оплаты из `GetState` (таймаут 8 с, не мешает ответу `OK`) | новая | нет | добавь функцию целиком (рядом с похожими) |
| 5 | `_finalize_credited_payment` — метка «T-Bank» в логе, поле `provider` в событиях, для тестовых платежей (`paid['test']`) не шлёт покупочные события | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 6 | `yookassa_status` — опрос статуса: за статусом ходит `payment_gateway.get_payment_status(row)` по провайдеру строки | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |

**Уровень модуля** (импорты и константы — добавь недостающее):

* import: asyncio
* import: bot_pay, payment_gateway, tbank_acq
* constant: TBANK_METHOD_TIMEOUT

## Чего не хватает у вас (проверено по снимку вашего репозитория от 09.11 — перепроверь)

* `tbank_notify` использует то, чего нет в вашем файле/дереве: `yookassa_repo.record_refund` (нет в вашем `app.db.yookassa_repo`; есть в нашем файле из кита — переноси вместе); `yookassa_repo.reopen_canceled` (нет в вашем `app.db.yookassa_repo`; есть в нашем файле из кита — переноси вместе); `yookassa_repo.save_payment_method` (нет в вашем `app.db.yookassa_repo`; есть в нашем файле из кита — переноси вместе)
* `_save_tbank_method` использует то, чего нет в вашем файле/дереве: `yookassa_repo.save_payment_method` (нет в вашем `app.db.yookassa_repo`; есть в нашем файле из кита — переноси вместе)

Модули кита (`tbank_acq`, `payment_gateway`, `bot_pay`, `pay_ticket`, `tbank_methods`, `miniapp_pay`) лежат рядом — копируй их первыми. Остальное — это модули/функции нашего форка, которых у вас нет: **не переноси** вызовы, которые к Т-Банку не относятся (рефералка, скидочная лесенка, Метрика, пиксель VK и т. п.), а недостающее по делу замени своим аналогом.

## Важные оговорки

* Файл большой, Т-Банка в нём шесть блоков (таблица выше). Остальное (Stars, Robokassa, lava.top, офферы, рефералка) — не ваше дело, не трогай.
* Путь нотификаций: у вас уже есть `POST /tbank/webhook` с вашей логикой. Наш обработчик называется `tbank_notify` и висит на `/tbank/notify`. Проще всего **повесить наш на ваш путь** (`@router.post("/tbank/webhook")`) и удалить ваш — тогда URL в кабинете Т-Банка менять не нужно. Платежи «в полёте» с `tb_`-префиксом — см. `AGENT.md`, раздел «Платежи в полёте».
* `yookassa_create` у нас ссылается на `_offer_slug`, `_menu_bonus_generations`, `_validate_payment_amount` — это наши (скидки/бонусы меню), у вас может быть иначе: меняй **только** строки, помеченные `+` в диффе (выбор провайдера, `payment_gateway.create_payment`, `provider`, `provider_order_id`, `bot_pay.fields_for_bot`).
* Импорт `referral_notify`, `photo_rating`, `discount_ladder`, `gift_push` и т. п. — это наш форк; не переноси.

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `import: asyncio`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+import asyncio
```

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
@@ -4,6 +4,6 @@
     pool: asyncpg.Pool = Depends(get_db),
 ):
-    """Create a YooKassa SBP payment and return confirmation URL."""
-    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
+    """Create a card payment (YooKassa SBP, or T-Bank by the provider flag) and return the confirmation URL."""
+    if not payment_gateway.yookassa_configured() and not tbank_acq.is_configured():
         raise HTTPException(status_code=503, detail="YooKassa not configured")
 
@@ -12,4 +12,7 @@
         raise HTTPException(status_code=404, detail="User not found")
     user = user_data["user"]
+    provider = payment_gateway.choose_provider(user["id"])
+    if not payment_gateway.configured(provider):
+        raise HTTPException(status_code=503, detail="YooKassa not configured")
 
     generations, amount_rubles = await _validate_payment_amount(pool, body)
@@ -25,5 +28,6 @@
         )
 
-    result = await yookassa_service.create_sbp_payment(
+    result = await payment_gateway.create_payment(
+        provider,
         amount_rubles=amount_rubles,
         description=f"{settings.BRAND_NAME} — пакет {generations} энергии",
@@ -31,4 +35,6 @@
         user_id=user["id"],
         telegram_id=body.telegram_id,
+        # T-Bank: the flags that let the buttons of the bot ask for the SberPay / T-Pay link of this very payment.
+        device_data=bot_pay.BOT_DEVICE_DATA if provider == payment_gateway.TBANK and settings.TBANK_BUTTONS else None,
     )
 
@@ -44,4 +50,6 @@
         offer_slug=_offer_slug(body.context),
         bonus_generations=bonus_generations,
+        provider=result.get("provider", "yookassa"),
+        provider_order_id=result.get("order_id"),
     )
 
@@ -60,4 +68,5 @@
             "yookassa_id": result["yookassa_id"],
             "offer_slug": _offer_slug(body.context),
+            "provider": result.get("provider", "yookassa"),
         },
     )
@@ -69,3 +78,8 @@
         "amount_rubles": amount_rubles,
         "bonus_generations": bonus_generations,
+        # T-Bank with the buttons on: ``pay_buttons`` (SberPay · SBP · T-Pay · card) and the page with the same choice as the
+        # ``confirmation_url`` of a bot that does not know the list. The stored row keeps the bank's own form.
+        **await bot_pay.fields_for_bot(
+            provider=result.get("provider"), payment_id=result["yookassa_id"], user_id=user["id"], platform="tg",
+        ),
     }
```

### `yookassa_webhook`

```diff
--- yookassa_webhook (до Т-Банка)
+++ yookassa_webhook (с Т-Банком)
@@ -26,4 +26,7 @@
     if local_payment is None:
         logger.warning("YooKassa webhook: unknown payment id=%s", yookassa_id)
+        return {"ok": True}
+    if payment_gateway.provider_of(local_payment) != payment_gateway.YOOKASSA:
+        logger.warning("YooKassa webhook: id=%s belongs to another provider, ignored", yookassa_id)
         return {"ok": True}
```

### `tbank_notify`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `constant: TBANK_METHOD_TIMEOUT`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+TBANK_METHOD_TIMEOUT = 8.0
```

### `_save_tbank_method`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `_finalize_credited_payment`

```diff
--- _finalize_credited_payment (до Т-Банка)
+++ _finalize_credited_payment (с Т-Банком)
@@ -18,5 +18,6 @@
     is_stars = method == "telegram_stars"
     is_lava = method == "lava"
-    label = "Stars" if is_stars else "lava.top" if is_lava else "YooKassa"
+    is_tbank = paid.get("provider") == "tbank"
+    label = "Stars" if is_stars else "lava.top" if is_lava else "T-Bank" if is_tbank else "YooKassa"
     ref = paid.get("yookassa_id") or paid.get("payload") or paid.get("contract_id")
     remaining = paid["new_balance"]
@@ -104,4 +105,8 @@
                     )
 
+    if paid.get("test"):
+        # Test-terminal payment (status test_owner): no purchase events, nothing for the funnel or the ad systems.
+        logger.info("T-Bank TEST payment credited, purchase analytics skipped: ref=%s", ref)
+        return paid
     await analytics.track(
         "stars_payment_succeeded" if is_stars
@@ -121,4 +126,5 @@
             "source": source,
             "offer_slug": paid.get("offer_slug"),
+            "provider": "tbank" if is_tbank else "yookassa",
         },
     )
@@ -133,5 +139,5 @@
             ),
             "currency": "XTR" if is_stars else paid["currency"] if is_lava else "RUB",
-            "method": method,
+            "method": "tbank" if is_tbank else method,
             "amount_rubles": paid["amount_rubles"],
             "generations": paid["generations"],
```

### `yookassa_status`

```diff
--- yookassa_status (до Т-Банка)
+++ yookassa_status (с Т-Банком)
@@ -23,5 +23,5 @@
     for pending in all_pending:
         try:
-            yk_status = await yookassa_service.get_payment_status(pending["yookassa_id"])
+            yk_status = await payment_gateway.get_payment_status(pending)
             remote_status = yk_status.get("status")
             logger.info(
@@ -31,5 +31,5 @@
 
             if remote_status == "succeeded":
-                if not yookassa_service.matches_local_payment(yk_status, pending):
+                if not payment_gateway.matches_local_payment(yk_status, pending):
                     logger.error(
                         "YooKassa payment details mismatch: yookassa_id=%s user=%s",
```
