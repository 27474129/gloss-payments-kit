# telegram-bot/core/flows/payment.py — что здесь наше

Файл `telegram-bot/core/flows/payment.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

Сверка с вашим файлом сделана по снимку репозитория от 09.11 (TG-бот — от 25.09): это подсказка, а не истина.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `checkout_pay_buttons` — разбор `pay_buttons` из ответа backend | новая | нет | добавь функцию целиком (рядом с похожими) |
| 2 | `pay_button_rows` — раскладка кнопок 2×2 | новая | нет | добавь функцию целиком (рядом с похожими) |
| 3 | `payment_link_keyboard` — клавиатура с кнопками способов вместо одной «Оплатить» | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |
| 4 | `handle_callback` — передаёт кнопки способов (`pay_buttons` из ответа) в клавиатуру со ссылкой | изменена | есть | замени тело вашей функции на нашу (смотри дифф ниже: наше — только строки с `+`), сохрани свои правки вне Т-Банка |

**Уровень модуля** (импорты и константы — добавь недостающее):

* constant: PAY_BUTTON_MARKS
* constant: PAY_BUTTONS_PER_ROW

## Дифф: было до Т-Банка → стало (по блокам)

Читай как инструкцию, что именно добавилось/изменилось. Строки `-` — как было, `+` — как стало.

### `constant: PAY_BUTTON_MARKS`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+PAY_BUTTON_MARKS = {"sberpay": "🟢", "sbp": "⚪", "tpay": "🟡", "card": "💳"}
```

### `constant: PAY_BUTTONS_PER_ROW`

```diff
--- до
+++ после
@@ -0,0 +1 @@
+PAY_BUTTONS_PER_ROW = 2
```

### `checkout_pay_buttons`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `pay_button_rows`

```diff
(новая функция/блок — целиком, см. маркеры в файле)
```

### `payment_link_keyboard`

```diff
--- payment_link_keyboard (до Т-Банка)
+++ payment_link_keyboard (с Т-Банком)
@@ -1,5 +1,8 @@
-def payment_link_keyboard(url: str) -> Keyboard:
+def payment_link_keyboard(url: str, pay_buttons: list[tuple[str, str]] | None = None) -> Keyboard:
+    link_rows = [
+        [Button.link(text, link) for text, link in row] for row in pay_button_rows(pay_buttons or [])
+    ] or [[Button.link("Оплатить", url)]]
     return Keyboard.from_rows([
-        [Button.link("Оплатить", url)],
+        *link_rows,
         [Button.callback(PAY_CHECK_BUTTON, "pay_check")],
     ])
```

### `handle_callback`

```diff
--- handle_callback (до Т-Банка)
+++ handle_callback (с Т-Банком)
@@ -20,5 +20,5 @@
         await context.send_text(
             PAY_OPEN_LINK,
-            keyboard=payment_link_keyboard(payment["confirmation_url"]),
+            keyboard=payment_link_keyboard(payment["confirmation_url"], checkout_pay_buttons(payment)),
         )
         return True
```
