/**
 * Адаптер MAX Bridge. Всё, что знает про `window.WebApp` (max-web-app.js).
 *
 * У MAX Bridge нет ни темы, ни цветов оболочки (в SDK нет colorScheme,
 * themeParams и setHeaderColor), поэтому цвета оболочки он не красит, а тему
 * не наследует: по умолчанию светлая, тёмная — по выбору в меню профиля.
 */

export type MaxWebApp = {
  ready?: () => void
  expand?: () => void
  close?: () => void
  /** SDK MAX принимает две строки, а не объект, как Telegram. */
  downloadFile?: (url: string, fileName: string) => Promise<unknown>
  openLink?: (url: string) => void
  openMaxLink?: (url: string) => void
  requestFullscreen?: () => void
  disableVerticalSwipes?: () => void
  initData: string
  initDataUnsafe?: {
    start_param?: string
    user?: { first_name?: string }
  }
  startParam?: string
  HapticFeedback?: {
    impactOccurred: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void
  }
}

export function getMaxWebApp(): MaxWebApp | undefined {
  return window.WebApp as MaxWebApp | undefined
}

/** Открывает max.ru-ссылку (бот, канал) внутри MAX. */
export function openMaxInternalLink(url: string) {
  const max = getMaxWebApp()
  // A max.ru URL is an internal MAX deep link.  openLink routes it through an
  // external browser, where the user loses the active Mini App context.
  // Keep the old bridge as a compatibility fallback for older clients.
  if (max?.openMaxLink) {
    max.openMaxLink(url)
    return
  }
  if (max?.openLink) {
    max.openLink(url)
    return
  }
  window.location.assign(url)
}
