# PaywallScreen.tsx — шторка «Пополнить энергию»

Файл целиком наш. У вас в `miniapp-ui` экрана оплаты нет, поэтому это **новый файл**, а не правка: копируй в `miniapp-ui/src/PaywallScreen.tsx`.

Что делает: шаг 1 — пакеты и «Оплатить»; шаг 2 — способы (SberPay с суммой, ряд СБП · T-Pay · Картой); форма карты Т-Банка в `<iframe>` внутри шторки;
экран ожидания (опрос статуса каждые 3 с, «Открыть форму оплаты», «Открыть в браузере», «Я оплатил — проверить»); возврат из банка по `postMessage`.

Импортирует (проверь, что есть у вас):
* `@/components/BottomSheet`, `@/components/Energy`, `@/components/PayButtons` — **копируй** (лежат рядом);
* `@/components/ui/button`, `@/components/ui/sonner` (`showMiniAppToast`), `@/lib/utils` — **у вас есть, не копируй**;
* `@/lib/pay`, `@/lib/paywall` — копируй;
* `@/platform/bridge` (`BOT_START`, `openBotDeepLink`, `openExternalLink`) — возьми прослойку `miniapp-ui/src/platform/bridge.ts` из этого репозитория.
* npm: `react`, `lucide-react`, `radix-ui`, `class-variance-authority`, `clsx`, `tailwind-merge`, `sonner` — все уже есть в `miniapp-ui/package.json` с теми же версиями.

Цветовые токены Tailwind, которых нет в вашем `index.css`: `--color-outline`, `--color-brand-text`, `--color-energy-plate` (значения — в `styles/tokens.css`: `--outline #1f1442`, `--brand-text #4a2be0`, `--energy-plate #5b3df5`; подставь свои по дизайну).

Подключение в корне приложения (`App.tsx` источника, строки ≈23, 113, 324, 335):
```tsx
import PaywallScreen from "./PaywallScreen"
import { openPaywall, usePaywall } from "./lib/paywall"
const paywall = usePaywall()
// кнопка «пополнить»: onClick={() => openPaywall("header")}   (источники: "header" | "edit" | "toast" | "resume")
{paywall ? <PaywallScreen balance={balance} notice={paywall.notice} resume={paywall.resume} source={paywall.source} /> : null}
```
Правила, на которых всё держится (подробно в `GOTCHAS.md`): `openLink` — синхронно в обработчике тапа; платёж кошелька создаёт страница `pay-go` по подписанному билету; в iframe форма показывает только карту; статус всегда перечитывается с сервера.
