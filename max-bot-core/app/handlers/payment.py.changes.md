# max-bot-core/app/handlers/payment.py — что здесь наше

Файл `max-bot-core/app/handlers/payment.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

**Вашего аналога этого файла в нашем снимке нет** (зеркало вашего MAX-бота устарело и файла не содержит) — сверь с реальным репозиторием, прежде чем что-то переносить.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `_create_and_send_payment` — создаёт платёж и показывает кнопки способов 2×2 | изменена | не проверено | добавь/замени по аналогии |

**Уровень модуля** (импорты и константы — добавь недостающее):

* import: checkout_pay_buttons, pay_button_rows

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `import: checkout_pay_buttons, pay_button_rows`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+from core.flows.payment import checkout_pay_buttons, pay_button_rows
```

### `_create_and_send_payment`

```diff
--- _create_and_send_payment (до Т-Банка)
+++ _create_and_send_payment (с Т-Банком)
@@ -47,8 +47,10 @@
         callback_data="back_to_packages",
     )
-    keyboard = InlineKeyboardMarkup(inline_keyboard=[
-        [InlineKeyboardButton(text="Оплатить 💳", url=confirmation_url)],
-        [back_button],
-    ])
+    # T-Bank: SberPay · SBP · T-Pay · card instead of the single button (the backend sends ``pay_buttons`` only then).
+    pay_rows = [
+        [InlineKeyboardButton(text=text, url=link) for text, link in row]
+        for row in pay_button_rows(checkout_pay_buttons(result))
+    ] or [[InlineKeyboardButton(text="Оплатить 💳", url=confirmation_url)]]
+    keyboard = InlineKeyboardMarkup(inline_keyboard=[*pay_rows, [back_button]])
 
     payment_message = await _render_checkout_message(
```
