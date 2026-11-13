# lib/paywall.ts — состояние шторки, пакеты, билет, «ожидающий платёж»

Файл целиком наш, у вас такого нет: копируй в `miniapp-ui/src/lib/paywall.ts` вместе с `lib/pay.ts`.

Зависит от `@/lib/pay` (статус платежа, `PayError`, `payGoUrl`) и `@/platform/bridge` (`BOT_START`, `miniAppApiUrl`, `miniAppHeaders`, `openBotDeepLink`) — прослойка из `miniapp-ui/src/platform/bridge.ts`.
Ручки backend (все в `miniapp_pay.py`, префикс `/api/v1/miniapp`): `payments/packages`, `payments/ticket`, `payments/yookassa/create`, `paywall-opened`, `payments/{id}`, `payments/ticket/{ticket}`.
Если `MINIAPP_PAYWALL_INAPP=false`, `payments/packages` отвечает «экран выключен», и `openPaywall` уводит диплинком в бота (`BOT_START.buyGenerations`) — поведение «как раньше».
`localStorage`: ключ `app-pay-pending` (ожидающий платёж; дозавершается при возврате в приложение). Событие баланса: `app:balance`.
