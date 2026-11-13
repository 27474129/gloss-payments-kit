# max-bot-core/core/flows/payment.py — что здесь наше

Файл `max-bot-core/core/flows/payment.py` лежит **целиком** — это наша версия из источника. **Не перезаписывай им свой файл вслепую:** у вас файл разошёлся с нашим (свои правки с момента форка), а наша версия ссылается на модули, которых у вас может не быть. Наши правки помечены комментариями `# --- T-BANK ---` … `# --- /T-BANK ---` (одна строка — суффикс `# --- T-BANK ---`). Всё вне маркеров — как было до Т-Банка (наши прочие доработки, переносить не нужно).

**Вашего аналога этого файла в нашем снимке нет** (зеркало вашего MAX-бота устарело и файла не содержит) — сверь с реальным репозиторием, прежде чем что-то переносить.

## Что делать — по порядку

| # | Блок | Наше | У вас | Действие |
|---|---|---|---|---|
| 1 | `checkout_pay_buttons` — разбор `pay_buttons` из ответа backend | новая | не проверено | добавь/замени по аналогии |
| 2 | `pay_button_rows` — раскладка кнопок 2×2 | новая | не проверено | добавь/замени по аналогии |
| 3 | `payment_link_keyboard` — клавиатура с кнопками способов вместо одной «Оплатить» | изменена | не проверено | добавь/замени по аналогии |
| 4 | `handle_callback` — передаёт кнопки способов (`pay_buttons` из ответа) в клавиатуру со ссылкой | изменена | не проверено | добавь/замени по аналогии |

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
@@ -1,9 +1,12 @@
 def payment_link_keyboard(
     url: str, rubles: int | None = None, *, pricing_context: str | None = None,
+    pay_buttons: list[tuple[str, str]] | None = None,
 ) -> Keyboard:
     # No manual "check payment" button: the webhook (backed by a recovery
     # sweep) always credits and notifies on its own. The back button mirrors
     # Telegram's checkout card.
-    rows = [[Button.link(f"Оплатить {rubles} ₽" if rubles else "Оплатить", url)]]
+    rows = [
+        [Button.link(text, link) for text, link in row] for row in pay_button_rows(pay_buttons or [])
+    ] or [[Button.link(f"Оплатить {rubles} ₽" if rubles else "Оплатить", url)]]
     if pricing_context is not None:
         back = (
```

### `handle_callback`

```diff
--- handle_callback (до Т-Банка)
+++ handle_callback (с Т-Банком)
@@ -94,4 +94,5 @@
             keyboard=payment_link_keyboard(
                 payment["confirmation_url"], selected_rubles, pricing_context=pricing_context,
+                pay_buttons=checkout_pay_buttons(payment),
             ),
         )
```
