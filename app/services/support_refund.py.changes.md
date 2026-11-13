# app/services/support_refund.py — что здесь наше

Файл `app/services/support_refund.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `tbank_refund_id` — идентификатор возврата Т-Банка в `support_refunds.refund_id` | новая | нет | добавь функцию целиком (рядом с похожими) |
| 2 | `tbank_cancel` — возврат через `Cancel` с проверкой `GetState` после потерянного ответа (у `Cancel` нет ключа идемпотентности) | новая | нет | добавь функцию целиком (рядом с похожими) |
| 3 | `plan_refunds` — для платежей Т-Банка остаток к возврату = сумма − `refunded_rubles` | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 4 | `execute_refund` — для `provider='tbank'` вызывает `tbank_cancel` | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |

**Уровень модуля** (импорты и константы — добавь недостающее):

* """Refunds of the support inbox: the calculation by the offe
* import: tbank_acq

## Важные оговорки

* Возвраты по Т-Банку работают только вместе с колонкой `refunded_rubles` и `yookassa_repo.record_refund`.
* У вас в `plan_refunds` платежи Т-Банка помечены «вернуть из кабинета Т-Бизнеса» (`tbank.is_tbank_id(item["yookassa_id"])`). **Оставь эту ветку для старых `tb_`-платежей**, заменив условие на `str(item["yookassa_id"]).startswith("tb_")` и убрав `from app.services import tbank` (старый модуль удаляется). Для новых платежей (`provider = 'tbank'`) работает наша ветка: остаток к возврату = сумма − `refunded_rubles`.
* Блок про «ежедневные награды» из нашей версии намеренно убран (к Т-Банку не относится, таблицы у вас нет).
* `tbank_cancel` не упрощай: у `Cancel` нет ключа идемпотентности, повтор после потерянного ответа сначала читает `GetState`.

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `"""Refunds of the support inbox: the calculation by the offe`

```diff
--- до
+++ после
@@ -0,0 +1,12 @@
+"""Refunds of the support inbox: the calculation by the offer and the provider call (YooKassa refund,
+or T-Bank ``Cancel`` for payments with ``provider = 'tbank'``).
+
+The model only *calculates* (``plan_refunds``) and proposes; money moves exclusively from the operator's
+button (``routes/inbox.py`` → ``execute_refund``). The rule is clause 6.2 of the offer: for the unused part of a
+package the customer gets ``price × unused ⚡ / ⚡ in the package``; the commission of the payment service is
+NOT kept (``SUPPORT_REFUND_DEDUCT_FEE=1`` switches the deduction on — the share of the actual YooKassa
+commission, ``amount − income_amount``, that falls on the refunded part, or 3.5 % when YooKassa did not report it).
+
+Which ⚡ are "unused" from which package: spending is assumed to take the oldest energy first, so the
+balance that is left belongs to the newest packages (FIFO).
+"""
```

### `import: tbank_acq`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+from app.services import tbank_acq
```

### `tbank_refund_id`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `tbank_cancel`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `plan_refunds`

```diff
--- plan_refunds (до Т-Банка)
+++ plan_refunds (с Т-Банком)
@@ -8,5 +8,5 @@
         """
         SELECT p.id, p.yookassa_id, p.created_at, p.amount_rubles,
-               p.generations + p.bonus_generations AS generations, p.platform
+               p.generations + p.bonus_generations AS generations, p.platform, p.provider, p.refunded_rubles
         FROM yookassa_payments p
         WHERE p.user_id = $1 AND p.status = 'succeeded' AND p.amount_rubles > 0
@@ -35,10 +35,15 @@
         income = None
         left = None
-        try:
-            remote = await fetch_payment(item["yookassa_id"], transport=transport)
-            income = income_of(remote)
-            left = refundable_left(remote)
-        except RefundError as exc:
-            entry["warning"] = str(exc)
+        if item.get("provider") == "tbank":
+            # T-Bank reports no commission per payment (fallback percent applies only if the deduction is on);
+            # what can still be returned is the amount minus what we recorded as refunded.
+            left = money(item["amount_rubles"]) - money(item["refunded_rubles"] or 0)
+        else:
+            try:
+                remote = await fetch_payment(item["yookassa_id"], transport=transport)
+                income = income_of(remote)
+                left = refundable_left(remote)
+            except RefundError as exc:
+                entry["warning"] = str(exc)
         calc = calc_refund(
             price=item["amount_rubles"], package=item["generations"], unused=item["unused"], income_amount=income,
@@ -48,5 +53,6 @@
         amount = Decimal(calc["amount"])
         if left is not None and amount > left:
-            entry.update(refundable=False, note=f"ЮKassa позволяет вернуть только {left} ₽ (уже возвращено)")
+            who = "Т-Банк" if item.get("provider") == "tbank" else "ЮKassa"
+            entry.update(refundable=False, note=f"{who} позволяет вернуть только {left} ₽ (уже возвращено)")
         else:
             entry.update(refundable=amount > 0)
```

### `execute_refund`

```diff
--- execute_refund (до Т-Банка)
+++ execute_refund (с Т-Банком)
@@ -35,10 +35,20 @@
         if claimed is None:
             raise RefundError("Возврат уже выполняется")
+        payment = await pool.fetchrow(
+            "SELECT yookassa_id, provider, amount_rubles, refunded_rubles FROM yookassa_payments WHERE id = $1",
+            row["payment_id"],
+        )
         try:
-            provider = await create_refund(
-                row["yookassa_id"], money(row["amount_rub"]), idempotence_key=row["idempotence_key"],
-                description="Возврат за неиспользованную часть пакета (п. 6.2 оферты)",
-                transport=transport,
-            )
+            if payment is not None and payment["provider"] == "tbank":
+                provider = await tbank_cancel(
+                    pool, payment=dict(payment), amount=money(row["amount_rub"]),
+                    retry_after_unknown=row["status"] == "pending", transport=transport,
+                )
+            else:
+                provider = await create_refund(
+                    row["yookassa_id"], money(row["amount_rub"]), idempotence_key=row["idempotence_key"],
+                    description="Возврат за неиспользованную часть пакета (п. 6.2 оферты)",
+                    transport=transport,
+                )
         except RefundError as exc:
             if exc.unknown:
@@ -62,4 +72,6 @@
             "UPDATE support_refunds SET refund_id = $2, updated_at = NOW() WHERE id = $1", refund_row_id, refund_id,
         )
+    elif str(refund_id).startswith("tbank:"):
+        provider_status = "succeeded"  # T-Bank Cancel answered earlier; only the balance side is left
     else:
         provider = await fetch_refund(refund_id, transport=transport)
```
