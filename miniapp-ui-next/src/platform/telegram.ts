/**
 * Адаптер Telegram WebApp. Здесь — всё, что знает про `window.Telegram`:
 * подпись запуска, диплинки, тема, цвета нативной оболочки, хаптик.
 * Остальной UI ходит через platform/bridge.ts и про Telegram не знает.
 */

export type TelegramWebApp = {
  ready: () => void
  expand: () => void
  requestFullscreen?: () => void
  isVersionAtLeast?: (version: string) => boolean
  initData: string
  initDataUnsafe?: {
    start_param?: string
    user?: {
      first_name?: string
    }
  }
  colorScheme?: "light" | "dark"
  themeParams?: { bg_color?: string }
  setHeaderColor?: (color: string) => void
  setBackgroundColor?: (color: string) => void
  setBottomBarColor?: (color: string) => void
  onEvent?: (event: string, handler: () => void) => void
  offEvent?: (event: string, handler: () => void) => void
  close?: () => void
  disableVerticalSwipes?: () => void
  openTelegramLink?: (url: string) => void
  openLink?: (url: string) => void
  /** Bot API 8.0: системный диалог сохранения файла. */
  downloadFile?: (params: { url: string; file_name: string }, callback?: (accepted: boolean) => void) => void
  HapticFeedback?: {
    impactOccurred: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void
  }
}

export function getTelegramWebApp(): TelegramWebApp | undefined {
  return window.Telegram?.WebApp as TelegramWebApp | undefined
}

let cachedTelegramInitData: string | undefined

/** Подписанные Telegram данные запуска. Обычно их отдаёт SDK, но если
 *  `telegram-web-app.js` не загрузился (сеть, блокировка), Telegram всё равно
 *  передаёт их в `#tgWebAppData=…` адреса — бэкенд проверяет подпись сам. */
export function getTelegramInitData(): string {
  const fromSdk = getTelegramWebApp()?.initData
  if (fromSdk) return fromSdk
  if (cachedTelegramInitData === undefined) {
    try {
      cachedTelegramInitData = new URLSearchParams(window.location.hash.slice(1)).get("tgWebAppData") ?? ""
    } catch {
      cachedTelegramInitData = ""
    }
  }
  return cachedTelegramInitData
}

/** Красит нативную шапку, фон и нижнюю панель Telegram в цвет полотна —
 *  иначе над приложением остаётся полоса цвета клиента. Методы появились в
 *  разных версиях Bot API (6.1/7.10), поэтому каждый вызов защищён. */
export function paintTelegramChrome(color: string) {
  const telegram = getTelegramWebApp()
  for (const apply of [telegram?.setHeaderColor, telegram?.setBackgroundColor, telegram?.setBottomBarColor]) {
    try {
      apply?.call(telegram, color)
    } catch {
      // Старая версия клиента не знает метода или цвета — остаёмся с нативным.
    }
  }
}

export function openTelegramBotLink(url: string) {
  const telegram = getTelegramWebApp()
  if (telegram?.openTelegramLink) {
    telegram.openTelegramLink(url)
    window.setTimeout(() => telegram.close?.(), 150)
    return
  }
  window.location.assign(url)
}

/** Родное окно «поделиться» Telegram: ссылка + подпись, выбор чата — в клиенте. */
export function openTelegramShare(link: string, text: string) {
  const url = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`
  getTelegramWebApp()?.openTelegramLink?.(url)
}

export function initTelegramRuntime(telegram: TelegramWebApp | undefined) {
  if (telegram?.isVersionAtLeast?.("8.0")) {
    telegram.requestFullscreen?.()
  }
}
