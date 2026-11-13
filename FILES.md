# FILES.md — карта кита

Структура папок = структура Photogen. «Как есть» — копируй по тому же пути. «Слить» — у вас такой файл есть: иди по `<файл>.changes.md`, не перезаписывай. «Справка» — для чтения, не копируй.

| Путь | Что | Как |
|---|---|---|
| `app/services/tbank_acq.py` | клиент API v2: подпись, Init, GetState, Cancel, ссылки кошельков, TLS с российским УЦ | как есть |
| `app/services/tbank_methods.py` | проба терминала: какие способы реально отдают ссылку | как есть |
| `app/services/payment_gateway.py` | выбор провайдера и единая «дверь» create/status/match | как есть |
| `app/services/pay_ticket.py` | подписанный билет Mini App (`gp1.…`) | как есть |
| `app/services/bot_pay.py` | ключ и кнопки способов для ботов (`gb1.…`) | как есть |
| `app/api/routes/miniapp_pay.py` | весь API оплаты Mini App и страницы возврата/выбора | как есть + вырезки из `.changes.md` |
| `app/api/routes/payments.py` | бот TG: создание, нотификация, статус, зачисление | слить (`payments.py.changes.md`) |
| `app/api/routes/max.py` | бот MAX: создание платежа и статус | слить (`max.py.changes.md`) |
| `app/db/yookassa_repo.py` | провайдер в записи, test_owner, способ оплаты, возвраты | слить (`yookassa_repo.py.changes.md`) |
| `app/services/support_refund.py` | возврат через Cancel | слить (`support_refund.py.changes.md`) |
| `app/workers/pending_payments.py` | воркер зависших платежей по провайдеру строки | слить (`pending_payments.py.changes.md`) |
| `app/core/config.tbank-block.py.txt` | блок настроек для `config.py` (целиком `config.py` не кладём) | вставить блок |
| `app/db/migrations/versions/TEMPLATE_*.txt` | две миграции-шаблона (поставь свой `down_revision`) | переименовать и поставить цепочку |
| `app/certs/russian_trusted_ca.pem` | российский УЦ для securepay (у вас тот же) | уже есть |
| `app/static/pay/*.svg` | логотипы для страницы выбора в боте | как есть |
| `scripts/backfill_tbank_method.py` | дозапись типа способа у старых платежей | как есть |
| `tests/test_tbank_*.py, test_pay_ticket.py, test_miniapp_pay.py` | тесты (HTTP-моки) | как есть + фикстуры |
| `miniapp-ui-next/src/PaywallScreen.tsx, components/{PayButtons,BottomSheet,Energy}.tsx, lib/{paywall,pay}.ts, assets/pay/*` | шторка оплаты | копировать в `miniapp-ui/src/` |
| `miniapp-ui-next/src/components/ui/{button,sonner}.tsx, lib/utils.ts, platform/{max,telegram,bridge}.ts, index.css, styles/tokens.css, platform/platform.css` | зависимости нашей шторки | справка (у вас аналоги) |
| `miniapp-ui/src/platform/{bridge,telegram}.ts` | готовые прослойки к вашему `lib/miniapp.ts` + `openExternalLink` | как есть |
| `telegram-bot/core/flows/payment.py, app/handlers/payment.py` | кнопки 2×2 в чате TG (необязательно) | слить (`.changes.md`) |
| `max-bot-core/core/flows/payment.py, app/handlers/payment.py` | то же для MAX (необязательно) | слить (`.changes.md`) |
| `telegram-bot/tests/, max-bot-core/tests/ test_pay_buttons.py` | тесты кнопок | как есть |
