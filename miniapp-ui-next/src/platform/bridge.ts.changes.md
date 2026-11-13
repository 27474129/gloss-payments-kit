# platform/bridge.ts — мост Mini App (оригинал, для справки)

Файл целиком наш, но у вас то же самое уже лежит в `miniapp-ui/src/lib/miniapp.ts` (`BOT_START`, `miniAppApiUrl`, `miniAppHeaders`, `openBotDeepLink`,
`getMaxWebApp`, `getMiniAppPlatform`, `getTelegramInitData`, `getActiveWebApp` — проверено по снимку 09.11). **Недостаёт одной функции — `openExternalLink`.**
Поэтому не копируй этот файл, а используй готовые прослойки `miniapp-ui/src/platform/bridge.ts` и `miniapp-ui/src/platform/telegram.ts` из репозитория
(реэкспорт вашего `lib/miniapp.ts` + `openExternalLink`).

Единственный смысловой кусок, ради которого файл в ките, — `openExternalLink(url)`: https открывается через `openLink` клиента (Telegram/MAX) **только** если мост есть
и есть `initData` (на странице MAX скрипт моста грузится всегда, но вне клиента `openLink` молча ничего не делает); иначе — `window.open`. Вызывать надо **синхронно** в обработчике тапа.
Остальное в файле (тема, токены доступа, карточки запуска, шаринг) — специфика нашего приложения, к оплате не относится.
