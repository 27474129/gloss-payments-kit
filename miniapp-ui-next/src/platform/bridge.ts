/**
 * Единственный слой доступа к мессенджеру (Telegram / MAX) и бэкенду мини-аппа.
 *
 * UI импортирует только отсюда и про `window.Telegram` / `window.WebApp` не
 * знает. Различия платформ — в адаптерах telegram.ts и max.ts; CSS-различия
 * (safe-area, шапка MAX) — platform.css, который включается атрибутом
 * `html[data-platform]`, выставляемым здесь.
 */
import {
  getTelegramInitData,
  getTelegramWebApp,
  initTelegramRuntime,
  openTelegramBotLink,
  openTelegramShare,
  paintTelegramChrome,
} from "./telegram"
import { getMaxWebApp, openMaxInternalLink, type MaxWebApp } from "./max"

export { getMaxWebApp, getTelegramInitData, openMaxInternalLink }
export type { MaxWebApp }
export type { TelegramWebApp } from "./telegram"

export type MiniAppPlatform = "telegram" | "max" | "browser"
export type Theme = "dark" | "light"

/** Имя бота для диплинков — переопределяется `VITE_BOT_USERNAME` (тестовый
 *  бот, смена бренда), по умолчанию прод-бот. */
export const BOT_USERNAME = import.meta.env.VITE_BOT_USERNAME ?? "your_bot"

/** Имя MAX-бота для диплинков — переопределяется `VITE_MAX_BOT_USERNAME`. */
export const MAX_BOT_USERNAME = import.meta.env.VITE_MAX_BOT_USERNAME ?? ""

/** Параметры диплинков в бота — контракт с app/handlers/*.py, не UI-строки. */
export const BOT_START = {
  buyGenerations: "buy_generations",
  uploadProfilePhoto: "upload_photo",
  /** Вернуться к шагу онбординга, на котором человек остановился. */
  onboarding: "onboarding",
  uploadMalePhoto: "upload_male_photo",
  trend: (id: number) => `fx_${id}`,
  /** Промо-баннеры на главном экране — ветки в app/handlers/onboarding.py. */
  editPhoto: "edit_photo",
  animate: "animate",
  enhance: "enhance",
  /** «Скопировать стиль» — повторить образ с любой картинки. */
  copyLook: "copy_look",
} as const

let cachedAccessToken: string | undefined
const ACCESS_TOKEN_STORAGE_KEY = "app-mini-access"

/** Токен доступа мини-аппа — из хэша, query или `tgWebAppStartParam`, с
 *  фолбэком на `sessionStorage`.
 *
 *  Токен несёт не только telegram_id, но и режим `preview` — бэкенд
 *  показывает тестовый контент (весь каталог трендов/видео сейчас
 *  помечен как тестовый) только когда видит его в заголовке
 *  `X-Mini-App-Access`. Telegram может перезапустить WebView в фоне
 *  (сворачивание, нехватка памяти) — JS-контекст и `sessionStorage` могут
 *  исчезнуть. Отдельные клиенты Telegram при этом не передают `initData` при
 *  повторном открытии. Поэтому `mini_access` намеренно остаётся в URL: это
 *  короткоживущий (24 часа), подписанный и привязанный к пользователю
 *  резервный способ восстановить сессию, а не декоративный параметр. */
export function getMiniAppAccessToken(): string {
  if (cachedAccessToken !== undefined) return cachedAccessToken
  const searchParams = new URLSearchParams(window.location.search)
  const hashParams = new URLSearchParams(window.location.hash.slice(1))
  const max = getMaxWebApp()
  const maxToken = getTelegramInitData() ? undefined : [
    max?.initDataUnsafe?.start_param,
    max?.startParam,
    searchParams.get("startapp"),
    searchParams.get("start_param"),
    searchParams.get("tgWebAppStartParam"),
    hashParams.get("startapp"),
    hashParams.get("tgWebAppStartParam"),
  // `?startapp=<метка>` из рекламы или поста — не токен: отправленный как
  // токен, он давал 401 вместо входа по подписанным initData MAX.
  ].find((value): value is string => Boolean(value && isAccessTokenShape(value)))
  // Служебный параметр запуска (`songs`, метка рекламы) не токен: как токен он ломал бы вход по initData.
  const queryStartParam = searchParams.get("tgWebAppStartParam")
  const fromUrl = maxToken
    ?? hashParams.get("mini_access")
    ?? searchParams.get("mini_access")
    ?? (queryStartParam && isAccessTokenShape(queryStartParam) ? queryStartParam : undefined)
  let token = fromUrl ?? ""
  try {
    if (token) {
      window.sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, token)
    } else {
      token = window.sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY) ?? ""
    }
  } catch {
    // sessionStorage недоступен (приватный режим и т.п.) — остаёмся с тем,
    // что нашли в URL.
  }
  cachedAccessToken = token
  return token
}

/** Формы токенов, которые выдаёт бэкенд (app/api/routes/miniapp.py):
 *  `m_…` (Telegram), `x_…` (MAX) и старый `id.issued.mode.sig`. */
function isAccessTokenShape(value: string): boolean {
  return /^[mx]_/.test(value) || value.split(".").length === 4
}

function isMaxAccessToken(token: string): boolean {
  return token.startsWith("x_") || token.startsWith("max")
}

function hasMaxUrlLaunchData(): boolean {
  const searchParams = new URLSearchParams(window.location.search)
  const hashParams = new URLSearchParams(window.location.hash.slice(1))
  return Boolean(
    searchParams.get("startapp")
      || searchParams.get("start_param")
      || searchParams.get("tgWebAppStartParam")
      || hashParams.get("startapp")
      || hashParams.get("tgWebAppStartParam")
  )
}

export function getMiniAppPlatform(): MiniAppPlatform {
  if (getTelegramInitData()) return "telegram"

  const max = getMaxWebApp()
  const maxLaunchData = Boolean(
    max?.initData
      || max?.initDataUnsafe?.start_param
      || max?.startParam
  )
  if (max && (maxLaunchData || hasMaxUrlLaunchData()) || isMaxAccessToken(getMiniAppAccessToken())) {
    return "max"
  }
  return "browser"
}

function getActiveWebApp() {
  return getMiniAppPlatform() === "max" ? getMaxWebApp() : getTelegramWebApp()
}

/** Открывает ссылку за пределами Mini App: оплату в банке, страницу провайдера.
 *  https идёт через `openLink` клиента (Telegram и MAX) — это внешний браузер, из которого открываются и
 *  диплинки банков. Ссылку SberPay (`sberpay://…`, WebView MAX её не открывает) бэкенд уже заворачивает в
 *  https-страницу `pay-open`; голая кастомная схема сюда попадает только как запасной случай — переходом окна. */
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

/** Имя пользователя из подписанных данных запуска — для приветствия. */
export function getGreetingName(): string | undefined {
  const name = getMiniAppPlatform() === "max"
    ? getMaxWebApp()?.initDataUnsafe?.user?.first_name
    : getTelegramWebApp()?.initDataUnsafe?.user?.first_name
  return name?.trim() || undefined
}

/* ------------------------------ тема --------------------------------------- */

// Раньше здесь жил ручной выбор темы (ключ app-theme-choice). Переключателя
// больше нет: приложение всегда светлое, а оставшийся в WebView ключ удаляем.
// Старый «app-theme» (прод /app/ на том же origin) не трогаем.
const LEGACY_THEME_KEY = "app-theme-choice"

/** Приложение всегда светлое, что бы ни стояло в клиенте: ночной Telegram и
 *  тёмная система в MAX его не затемняют. Тёмные токены в CSS не применяются. */
const THEME: Theme = "light"

/** Цвет полотна из tokens.css: нативную шапку Telegram красим в него. */
const CANVAS = "#fff6ea"

/** Ставит светлую тему на <html>, красит оболочку мессенджера под полотно и
 *  чистит сохранённый ранее выбор темы. */
export function applyTheme() {
  document.documentElement.dataset.theme = THEME
  if (getMiniAppPlatform() === "telegram") paintTelegramChrome(CANVAS)
  try {
    window.localStorage.removeItem(LEGACY_THEME_KEY)
  } catch {
    // Хранилище недоступно — нечего чистить.
  }
}

/* ------------------------------ запуск и среда ----------------------------- */

/** Stamp the host platform on <html> so platform.css can adjust chrome-dependent
 *  spacing. MAX draws its own native header and already consumes the device
 *  safe-area, so the Telegram-tuned `env(safe-area-inset-top)` padding would
 *  double-count and leave a dead band under MAX's header. Runs at module load
 *  (before first paint) to avoid a layout flash. */
function stampPlatform() {
  try {
    document.documentElement.dataset.platform = getMiniAppPlatform()
  } catch {
    // document not ready / SSR — the useEffect path re-stamps.
  }
}
stampPlatform()

/** Светлая тема на <html> до первого рендера React. */
export function stampInitialTheme() {
  try {
    applyTheme()
  } catch {
    // document not ready — initMiniAppRuntime поставит тему ещё раз.
  }
}

export function initMiniAppRuntime() {
  stampPlatform()
  const platform = getMiniAppPlatform()
  const telegram = getTelegramWebApp()
  // If the MAX SDK is present without launch fields, still initialize it; the
  // SDK is designed to be harmless outside MAX and this keeps the guards local.
  const webApp = platform === "max" ? getMaxWebApp() : telegram ?? getMaxWebApp()
  webApp?.ready?.()
  webApp?.expand?.()

  if (platform === "telegram") {
    initTelegramRuntime(telegram)
  } else {
    getMaxWebApp()?.requestFullscreen?.()
  }
  webApp?.disableVerticalSwipes?.()
  // Нативная шапка Telegram — в цвет светлого полотна (stamp мог пройти до ready).
  applyTheme()
}

export function closeMiniApp() {
  getActiveWebApp()?.close?.()
}

/** Заголовки, которые бэкенд мини-аппа проверяет на каждом запросе. */
export function miniAppHeaders(): Record<string, string> {
  return {
    "X-Mini-App-Access": getMiniAppAccessToken(),
    "X-Telegram-Init-Data": getTelegramInitData(),
    "X-Max-Init-Data": getMaxWebApp()?.initData ?? "",
  }
}

/** Относительный путь к API мини-аппа: страница живёт на `/app/`, `/max/`
 *  (и их `-next`), API — на том же origin уровнем выше. */
export function miniAppApiUrl(path: string): string {
  return `${import.meta.env.VITE_API_BASE || "../api/v1/miniapp/"}${path}`
}

/** Сохраняет готовое фото на устройство: родной downloadFile клиента, а если
 *  его нет (старый клиент) — открытие ссылки во внешнем браузере. */
export async function saveResultFile(url: string, name?: string): Promise<void> {
  const fileName = name || new URL(url, window.location.href).pathname.split("/").pop() || "photo.jpg"
  const max = getMaxWebApp()
  if (getMiniAppPlatform() === "max" && max?.downloadFile) {
    try {
      await max.downloadFile(url, fileName)
      return
    } catch {
      // Клиент отдаёт метод, но мост его не поддерживает — запасной путь ниже.
    }
  }
  const telegram = getTelegramWebApp()
  if (getMiniAppPlatform() === "telegram" && telegram?.downloadFile && telegram.isVersionAtLeast?.("8.0")) {
    await new Promise<void>((resolve, reject) => {
      telegram.downloadFile?.({ url, file_name: fileName }, (accepted) => (accepted ? resolve() : reject(new Error("declined"))))
    })
    return
  }
  openExternalLink(url)
}

/** Открывает диплинк в боте. В Telegram — через `openTelegramLink` с
 *  закрытием мини-аппа следом, вне Telegram — обычная навигация. */
export function openBotDeepLink(startParameter: string) {
  if (getMiniAppPlatform() === "max") {
    openMaxInternalLink(`https://max.ru/${MAX_BOT_USERNAME}?start=${encodeURIComponent(startParameter)}`)
    return
  }
  openTelegramBotLink(`https://t.me/${BOT_USERNAME}?start=${encodeURIComponent(startParameter)}`)
}

/** Параметр диплинка для того, кто открыл мини-апп раньше бота (403 каталога).
 *  Бот пишет его в `users.source` только при первой регистрации, поэтому сюда
 *  идёт рекламная метка, с которой человек пришёл (`photokeys_eg` и т.п.), —
 *  иначе он записывается как `onboarding` и объявление теряется. Без метки —
 *  прежний `onboarding`: бот начнёт знакомство или продолжит его. */
export function firstVisitStartParameter(): string {
  if (getMiniAppPlatform() !== "max") return BOT_START.onboarding
  const max = getMaxWebApp()
  const startParam = max?.initDataUnsafe?.start_param ?? max?.startParam ?? ""
  // Ссылки из бота несут подписанный токен доступа, а не метку источника.
  // `songs` — вход во вкладку «Песни», а не рекламная метка.
  const label = isAccessTokenShape(startParam) || startParam === "songs" ? "" : startParam.replace(/[^A-Za-z0-9_-]/g, "")
  // Кнопка поста канала (`look_5_c3`) — не рекламная метка: источник такого человека — канал.
  if (LAUNCH_CARD_PARAM.test(label)) return "channel"
  return label.slice(0, 64) || BOT_START.onboarding
}

const LAUNCH_CARD_PARAM = /^(trend|look)_(\d{1,9})(?:_([a-z0-9]{1,24}))?$/

export type LaunchCard = { kind: "trend" | "look"; id: number; startParam: string }

let cachedLaunchCard: LaunchCard | null | undefined

/** Карточка из кнопки поста канала: `look_<id>[_<пост>]` (вкладка «Образы») или `trend_<id>[_<пост>]` («Тренды»).
 *  В MAX метка приходит в `start_param` кнопки open_app, в Telegram — в `?startapp=`. Без такой метки — null. */
export function getLaunchCard(): LaunchCard | null {
  if (cachedLaunchCard !== undefined) return cachedLaunchCard
  const searchParams = new URLSearchParams(window.location.search)
  const hashParams = new URLSearchParams(window.location.hash.slice(1))
  const candidates = [
    getMaxWebApp()?.initDataUnsafe?.start_param,
    getMaxWebApp()?.startParam,
    getTelegramWebApp()?.initDataUnsafe?.start_param,
    searchParams.get("startapp"),
    searchParams.get("start_param"),
    searchParams.get("tgWebAppStartParam"),
    // MAX кладёт метку кнопки и в адрес окна: `?WebAppStartParam=look_5_g1`.
    searchParams.get("WebAppStartParam"),
    hashParams.get("startapp"),
    hashParams.get("tgWebAppStartParam"),
  ]
  cachedLaunchCard = null
  for (const value of candidates) {
    const match = value ? LAUNCH_CARD_PARAM.exec(value) : null
    if (match) {
      cachedLaunchCard = { kind: match[1] as "trend" | "look", id: Number(match[2]), startParam: value as string }
      break
    }
  }
  return cachedLaunchCard
}

let launchReported = false

/** Сообщает бэкенду об открытии по кнопке поста (учёт постов); один раз за запуск, сбой интерфейсу не мешает. */
export function reportLaunch(card: LaunchCard | null) {
  if (!card || launchReported) return
  launchReported = true
  fetch(miniAppApiUrl("launch"), {
    method: "POST",
    headers: { ...miniAppHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ start_param: card.startParam }),
  }).catch(() => undefined)
}

/** Диплинк в MAX-бота из-под другой платформы (кросс-промо) — в отличие
 *  от `openBotDeepLink`, не подстраивается под текущий хост, а всегда
 *  ведёт в MAX. */
export function openMaxCrossPromoLink(startParameter: string) {
  const url = `https://max.ru/${MAX_BOT_USERNAME}?start=${encodeURIComponent(startParameter)}`
  const telegram = getTelegramWebApp()
  if (telegram?.openLink) {
    telegram.openLink(url)
    return
  }
  window.open(url, "_blank", "noopener")
}

/** Есть ли родное окно «поделиться»: в Telegram — да (и при входе по m_-токену,
 *  когда initData пуст и платформа видится как browser), в MAX его нет — там
 *  ссылку копируют. */
export function canShareNatively(): boolean {
  return getMiniAppPlatform() !== "max" && Boolean(getTelegramWebApp()?.openTelegramLink)
}

/** Открывает окно «поделиться» Telegram с ссылкой и подписью. */
export function shareLink(link: string, text: string) {
  openTelegramShare(link, text)
}

/** Лёгкий тактильный отклик при открытии карточки — вне мессенджера
 *  (браузер для превью) `HapticFeedback` просто нет, вызов ничего не делает. */
export function hapticCardOpen() {
  (getActiveWebApp() ?? getMaxWebApp())?.HapticFeedback?.impactOccurred("light")
}
