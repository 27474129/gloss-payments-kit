import { getMaxWebApp, getMiniAppPlatform, getTelegramInitData, miniAppApiUrl, miniAppHeaders } from "@/platform/bridge"
import { getTelegramWebApp } from "@/platform/telegram"

/** Пакет, который открыл экран оплаты. Цену проверяет бэкенд по таблице тарифов. */
export type PayRoute = { generations: number; amount: number }

/** Кнопки способов на экране оплаты. `bank_card` и `widget` открывают виджет ЮKassa, `redirect` — обычную страницу ЮKassa (как у бота). */
export type PayMethod = "sberbank" | "sbp" | "tinkoff_bank" | "bank_card" | "widget" | "redirect"
/** `app` — диплинк в приложение банка (телефон), `web` — страница провайдера в браузере. */
export type PayFlow = "app" | "web"

export type PayCreated = {
  payment_id: string
  method: PayMethod
  /** `app_link` — ссылка способа Т-Банка (СБП, SberPay, T-Pay): её открывают снаружи, запускается приложение банка. */
  confirmation_type: "embedded" | "redirect" | "mobile_application" | "qr" | "app_link"
  /** Токен виджета (embedded) или null. */
  confirmation_token: string | null
  /** Диплинк банка или https-страница провайдера (не embedded) или null. */
  confirmation_url: string | null
  amount: number
  generations: number
  bonus_generations: number
  /** Кто принял платёж (`tbank` | `yookassa`). */
  provider?: string
  /** Страницу оплаты можно показать внутри приложения (iframe), а не уводить в браузер. */
  embed?: boolean
  /** Страница оплаты открывается в этом же окне приложения и сама возвращает обратно (`?pay=return`). */
  top_level?: boolean
}

export type PayStatus = {
  status: "pending" | "succeeded" | "canceled"
  generations: number
  amount: number
  credited_generations?: number
  balance?: number | null
}

/** Ошибка API с текстом, который можно показать человеку. */
export class PayError extends Error {}

/** Банк не отдал ссылку этого способа (он не подключён к терминалу): кнопку убираем, предлагаем другой способ. */
export class PayMethodUnavailable extends PayError {}

function parsePack(generations: string | null, amount: string | null): PayRoute | null {
  const g = Number(generations)
  const a = Number(amount)
  if (!Number.isInteger(g) || !Number.isInteger(a) || g <= 0 || a <= 0 || g > 10000 || a > 100000) return null
  return { generations: g, amount: a }
}

/** Экран оплаты открывают прямой ссылкой. Параметры ищем во всех местах, куда их
 *  кладут клиенты: Telegram и MAX перезаписывают `#hash` своими данными запуска,
 *  поэтому кроме `#/pay?gens=10&amount=299` понимаем `?screen=pay&gens=10&amount=299`
 *  и параметр запуска `startapp=pay_10_299`. */
export function readPayRoute(): PayRoute | null {
  const hash = window.location.hash.slice(1)
  if (hash.startsWith("/pay")) {
    const params = new URLSearchParams(hash.split("?")[1] ?? "")
    const route = parsePack(params.get("gens"), params.get("amount"))
    if (route) return route
  }

  const search = new URLSearchParams(window.location.search)
  if (search.get("screen") === "pay") {
    const route = parsePack(search.get("gens"), search.get("amount"))
    if (route) return route
  }

  const hashParams = new URLSearchParams(hash)
  const startParams = [
    getTelegramWebApp()?.initDataUnsafe?.start_param,
    getMaxWebApp()?.initDataUnsafe?.start_param,
    getMaxWebApp()?.startParam,
    hashParams.get("tgWebAppStartParam"),
    search.get("startapp"),
    search.get("tgWebAppStartParam"),
    search.get("start_param"),
  ]
  for (const value of startParams) {
    const match = /^pay_(\d+)_(\d+)$/.exec(value ?? "")
    const route = match ? parsePack(match[1], match[2]) : null
    if (route) return route
  }
  return null
}

async function detail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    return typeof body.detail === "string" ? body.detail : ""
  } catch {
    return ""
  }
}

/** Телефон: там работают диплинки в приложения банков. На компьютере сразу страница провайдера. */
export const isMobileDevice = () => /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)

export async function createPayment(route: PayRoute, method: PayMethod, flow: PayFlow = "app"): Promise<PayCreated> {
  let response: Response
  try {
    response = await fetch(miniAppApiUrl("payments/yookassa/create"), {
      method: "POST",
      cache: "no-store",
      headers: { ...miniAppHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ generations: route.generations, amount_rubles: route.amount, context: "menu", method, flow }),
    })
  } catch {
    throw new PayError("Нет связи. Проверь интернет и попробуй ещё раз.")
  }
  if (response.ok) return (await response.json()) as PayCreated
  if (response.status === 401 || response.status === 403) {
    throw new PayError("Открой YOUR_BRAND из бота — так мы узнаем, кому зачислить энергию.")
  }
  if (response.status === 400) {
    throw new PayError("Такого пакета сейчас нет. Выбери пакет в боте.")
  }
  if (response.status === 429) {
    throw new PayError("Слишком много неоплаченных попыток. Подожди пару минут и открой экран снова.")
  }
  throw new PayError(`Не удалось создать платёж${(await detail(response)) ? ` (${await detail(response)})` : ""}. Попробуй ещё раз.`)
}

/** Билет оплаты (`gp1.…`): подписанное разрешение начать платёж за пакет. Платежа за ним ещё нет — его создаёт страница `pay-go`. */
export const TICKET_PREFIX = "gp1."
/** Способы, которые открывают страницу `pay-go` прямо из тапа: SberPay, СБП, T-Pay и «страница банка». */
export type WalletWay = "sberpay" | "sbp" | "tpay" | "page"
export const WALLET_WAY: Partial<Record<PayMethod, WalletWay>> = { sberbank: "sberpay", sbp: "sbp", tinkoff_bank: "tpay", redirect: "page" }

/** Адрес страницы, которая по билету создаёт платёж и перекидывает на ссылку банка. Абсолютный: его открывает внешний браузер,
 *  от адреса приложения он отсчитывается тем же `miniAppApiUrl`, что и остальные запросы (превью `/res-api/…`, прод `/api/…`). */
export function payGoUrl(ticket: string, way: WalletWay): string {
  const query = `t=${encodeURIComponent(ticket)}&m=${way}&p=${getMiniAppPlatform() === "max" ? "max" : "tg"}`
  return new URL(miniAppApiUrl(`pay-go?${query}`), window.location.href).href
}

/** Статус платежа или билета: у билета — последний итог по всем его платежам (`pending`, пока платежа ещё нет). */
export async function fetchPaymentStatus(paymentId: string): Promise<PayStatus> {
  const path = paymentId.startsWith(TICKET_PREFIX) ? `payments/ticket/${encodeURIComponent(paymentId)}` : `payments/${encodeURIComponent(paymentId)}`
  const response = await fetch(miniAppApiUrl(path), {
    cache: "no-store",
    headers: miniAppHeaders(),
  })
  if (!response.ok) throw new PayError("status unavailable")
  return (await response.json()) as PayStatus
}

/** Телеграм-признак нужен экрану, чтобы подписать «Открой из бота» точнее. */
export const hasTelegramLaunch = () => Boolean(getTelegramInitData())

/* --------------------------- виджет ЮKassa ---------------------------------- */

const WIDGET_SRC = "https://yookassa.ru/checkout-widget/v1/checkout-widget.js"

export type CheckoutWidget = {
  render: (containerId: string) => Promise<void>
  destroy: () => void
  on: (event: "success" | "fail" | "complete" | "modal_close", callback: () => void) => void
}

type CheckoutWidgetConstructor = new (options: {
  confirmation_token: string
  error_callback: (error: unknown) => void
  customization?: { modal?: boolean; colors?: Record<string, string> }
}) => CheckoutWidget

let widgetScript: Promise<CheckoutWidgetConstructor> | undefined

/** Скрипт виджета грузится один раз за сессию. Неудачная загрузка не кэшируется. */
export function loadCheckoutWidget(): Promise<CheckoutWidgetConstructor> {
  widgetScript ??= new Promise<CheckoutWidgetConstructor>((resolve, reject) => {
    const existing = (window as unknown as { YooMoneyCheckoutWidget?: CheckoutWidgetConstructor }).YooMoneyCheckoutWidget
    if (existing) return resolve(existing)
    const script = document.createElement("script")
    script.src = WIDGET_SRC
    script.async = true
    script.onload = () => {
      const ctor = (window as unknown as { YooMoneyCheckoutWidget?: CheckoutWidgetConstructor }).YooMoneyCheckoutWidget
      if (ctor) resolve(ctor)
      else reject(new PayError("widget missing"))
    }
    script.onerror = () => reject(new PayError("widget script failed"))
    document.head.appendChild(script)
  }).catch((reason: unknown) => {
    widgetScript = undefined
    throw reason
  })
  return widgetScript
}
