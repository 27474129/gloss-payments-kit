# telegram-bot/app/handlers/payment.py — что здесь наше

Файл `telegram-bot/app/handlers/payment.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `_create_and_send_payment` — создаёт платёж и показывает кнопки способов 2×2 | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |

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
@@ -69,8 +69,10 @@
         callback_data="back_to_packages",
     )
-    keyboard = InlineKeyboardMarkup(inline_keyboard=[
-        [InlineKeyboardButton(text=f"Оплатить {rubles} ₽", url=confirmation_url)],
-        [back_button],
-    ])
+    # T-Bank: SberPay · SBP · T-Pay · card instead of the single button (the backend sends ``pay_buttons`` only then).
+    pay_rows = [
+        [InlineKeyboardButton(text=text, url=link) for text, link in row]
+        for row in pay_button_rows(checkout_pay_buttons(result))
+    ] or [[InlineKeyboardButton(text=f"Оплатить {rubles} ₽", url=confirmation_url)]]
+    keyboard = InlineKeyboardMarkup(inline_keyboard=[*pay_rows, [back_button]])
     package_label = f"{generations} ⚡ энергии · {rubles} ₽"
     if source == "discount" and offer_code == WINBACK_SLUG:
```
