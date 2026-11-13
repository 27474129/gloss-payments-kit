// Прослойка: ваш `lib/miniapp.ts` уже содержит всё, что нужно экрану оплаты, кроме `openExternalLink`.
// Положи файл в miniapp-ui/src/platform/bridge.ts (папку platform создай) — PaywallScreen/paywall/pay импортируют именно `@/platform/bridge`.
export {
  BOT_START,
  getActiveWebApp,
  getMaxWebApp,
  getMiniAppPlatform,
  getTelegramInitData,
  miniAppApiUrl,
  miniAppHeaders,
  openBotDeepLink,
} from "@/lib/miniapp"

import { getActiveWebApp } from "@/lib/miniapp"

/** Открывает ссылку за пределами Mini App: оплату в банке, страницу провайдера.
 *  https идёт через `openLink` клиента (Telegram и MAX) — внешний браузер, из которого открываются и диплинки банков.
 *  Ссылку SberPay (`sberpay://…`, WebView MAX её не открывает) backend уже заворачивает в https-страницу `pay-open`.
 *  Вызывать СИНХРОННО в обработчике тапа. */
export function openExternalLink(url: string) {
  if (!/^https?:\/\//i.test(url)) {
    window.location.href = url
    return
  }
  // Мост есть только внутри настоящего клиента: на странице MAX скрипт моста грузится всегда, и вне MAX
  // (превью по ссылке в браузере) его openLink молча ничего не делает — тогда открываем обычным окном.
  const app = getActiveWebApp()
  if (app?.openLink && app.initData) {
    try {
      app.openLink(url)
      return
    } catch {
      // Клиент отказался открывать — ниже запасной путь.
    }
  }
  window.open(url, "_blank", "noopener")
}
