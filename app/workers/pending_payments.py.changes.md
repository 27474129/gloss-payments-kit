# app/workers/pending_payments.py — что здесь наше

Файл `app/workers/pending_payments.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `_sweep` — воркер зависших платежей: статус по провайдеру строки, поле `provider` в событии | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |

**Уровень модуля** (импорты и константы — добавь недостающее):

* """Background worker that polls the card provider for stale 
* import: yookassa
* import: payment_gateway

## Важные оговорки

* Это воркер-страховка от потерянной нотификации (раз в ~2 минуты). Он обязан ходить за статусом через `payment_gateway.get_payment_status(row)` — по провайдеру **строки**, а не по текущему флагу.
* `referral_notify` и часть аналитики в файле — из нашего форка; Т-Банк касается только блока с `payment_gateway`.

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `"""Background worker that polls the card provider for stale `

```diff
--- до
+++ после
@@ -0,0 +1,7 @@
+"""Background worker that polls the card provider for stale pending payments.
+
+Runs every POLL_INTERVAL seconds, picks up pending payments older than
+MIN_AGE_SECONDS, checks their status at their provider (YooKassa API or
+T-Bank GetState, by ``provider`` of the row — the cover for a lost webhook),
+and credits generations if the payment succeeded.
+"""
```

### `import: yookassa`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+from app.services import yookassa as yookassa_service  # noqa: F401  (kept importable for tests)
```

### `import: payment_gateway`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+from app.services import payment_gateway
```

### `_sweep`

```diff
--- _sweep (до Т-Банка)
+++ _sweep (с Т-Банком)
@@ -13,9 +13,9 @@
         yookassa_id = p["yookassa_id"]
         try:
-            yk_status = await yookassa_service.get_payment_status(yookassa_id)
+            yk_status = await payment_gateway.get_payment_status(p)
             remote_status = yk_status.get("status")
 
             if remote_status == "succeeded":
-                if not yookassa_service.matches_local_payment(yk_status, p):
+                if not payment_gateway.matches_local_payment(yk_status, p):
                     logger.error("pending_payments: provider data mismatch for yookassa_id=%s", yookassa_id)
                     continue
@@ -102,4 +102,5 @@
                             "source": "background_poll",
                             "platform": platform,
+                            "provider": payment_gateway.provider_of(p),
                         },
                     )
```
