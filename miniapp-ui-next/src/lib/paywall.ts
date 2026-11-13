import { useSyncExternalStore } from "react"

import { fetchPaymentStatus, isMobileDevice, PayError, PayMethodUnavailable, type PayCreated, type PayMethod, type PayStatus } from "@/lib/pay"
import { BOT_START, miniAppApiUrl, miniAppHeaders, openBotDeepLink } from "@/platform/bridge"

/** Откуда открыли «Пополнить энергию» — уходит в событие `miniapp_paywall_opened`. Закрытый набор, как у бэкенда. */
export type PaywallSource = "header" | "edit" | "toast" | "resume"

export type PackageTier = {
  generations: number
  rubles: number
  /** Обычная цена, если у пакета скидка; иначе 0. */
  original_rubles: number
  discount_percent: number
  bonus_generations: number
}

export type SpecialOffer = {
  slug: string
  /** Контекст оплаты (`discount:<slug>`): цену по нему проверяет сервер. */
  context: string
  expires_at: string
  tiers: PackageTier[]
}

/** Кнопки способов оплаты Т-Банка. `wallets` — способы, на которые банк отдаёт ссылку (в порядке кнопок: СБП, SberPay, T-Pay);
 *  `bank_page` — ни один не подключён, вместо них одна кнопка «СБП и другие способы» (страница банка во внешнем окне). */
export type PayMethods = { wallets: PayMethod[]; card: boolean; bank_page: boolean }

/** Ответ `GET payments/packages`: тот же список, что показывает бот в меню покупки. */
export type Packages = {
  enabled: boolean
  tiers: PackageTier[]
  offer: SpecialOffer | null
  /** Куда уйдёт платёж: `tbank` | `yookassa` (подпись под кнопкой). */
  provider?: string
  /** Тестовый терминал Т-Банка (превью): деньги не списываются. */
  test_mode?: boolean
  /** Кнопки способов (только Т-Банк); null/нет — одна кнопка «Оплатить», как раньше. */
  pay_methods?: PayMethods | null
}

/** Сообщение страницы возврата банка (`pay-return`) родителю: форма оплаты закончилась. */
export const PAY_RETURN_MESSAGE = "app-pay-return"

/* ---------------------------- какой экран открыт ----------------------------- */

/** Плашка над списком пакетов, если вернулись из банка без оплаты. */
export type PaywallNotice = "declined" | "unfinished"

type PaywallState = { source: PaywallSource; resume: PendingPayment | null; notice: PaywallNotice | null } | null

let state: PaywallState = null
/** Флаг `MINIAPP_PAYWALL_INAPP` на сервере. null — ещё не знаем (первый ответ в пути). */
let paywallEnabled: boolean | null = null
const listeners = new Set<() => void>()

function emit() {
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function usePaywall(): PaywallState {
  return useSyncExternalStore(subscribe, () => state)
}

export function closePaywall() {
  state = null
  emit()
  if (paywallEnabled) refreshPackages()
}

/** Все входы (плашка энергии, «Пополнить» в «Изменить», «Выбрать пакет» в тосте) идут сюда.
 *  Флаг выключен — как раньше, диплинк в бота; ещё не знаем — открываем экран, он сам уйдёт в бота. */
export function openPaywall(source: PaywallSource, resume: PendingPayment | null = null, notice: PaywallNotice | null = null) {
  if (paywallEnabled === false) {
    openBotDeepLink(BOT_START.buyGenerations)
    return
  }
  state = { source, resume, notice }
  emit()
}

/** Экран увидел, что флаг выключен: запоминаем и отправляем покупателя в бота. */
export function paywallUnavailable() {
  paywallEnabled = false
  closePaywall()
  openBotDeepLink(BOT_START.buyGenerations)
}

/* ------------------------------ баланс: общий -------------------------------- */

const BALANCE_EVENT = "app:balance"

/** Платёж зачислен: каталог и шапки читают баланс из разных мест, им всем сообщаем новое число. */
export function announceBalance(balance: number) {
  window.dispatchEvent(new CustomEvent<number>(BALANCE_EVENT, { detail: balance }))
}

export function onBalanceAnnounced(listener: (balance: number) => void) {
  const handler = (event: Event) => listener((event as CustomEvent<number>).detail)
  window.addEventListener(BALANCE_EVENT, handler)
  return () => window.removeEventListener(BALANCE_EVENT, handler)
}

/* --------------------------------- запросы ----------------------------------- */

export async function fetchPackages(): Promise<Packages> {
  let response: Response
  try {
    response = await fetch(miniAppApiUrl("payments/packages"), { cache: "no-store", headers: miniAppHeaders() })
  } catch {
    throw new PayError("Нет связи. Проверь интернет и попробуй ещё раз.")
  }
  if (response.status === 401 || response.status === 403) {
    throw new PayError("Открой YOUR_BRAND из бота — так мы узнаем, кому зачислить энергию.")
  }
  if (!response.ok) throw new PayError("Не удалось загрузить пакеты. Попробуй ещё раз.")
  const packages = (await response.json()) as Packages
  paywallEnabled = packages.enabled
  return packages
}

const PACKAGES_FRESH_MS = 2 * 60_000
let packagesCache: { at: number; promise: Promise<Packages> } | null = null

/** Пакеты для шторки: ответ не старше 2 минут берём из памяти, без сети — шторка открывается сразу, без «Загружаем». */
export function loadPackages(): Promise<Packages> {
  if (packagesCache && Date.now() - packagesCache.at < PACKAGES_FRESH_MS) return packagesCache.promise
  const promise = fetchPackages()
  packagesCache = { at: Date.now(), promise }
  promise.catch(() => {
    if (packagesCache?.promise === promise) packagesCache = null
  })
  return promise
}

/** После оплаты или закрытия шторки: следующий показ возьмёт свежий список (спецпредложение могло смениться). */
export function refreshPackages() {
  packagesCache = null
  loadPackages().catch(() => undefined)
}

/** Один запрос при запуске: узнаём флаг и заранее грузим пакеты, чтобы тап по плашке сразу открывал шторку. Ошибки молча. */
export function prefetchPaywallFlag() {
  loadPackages().catch(() => undefined)
}

export function trackPaywallOpened(source: PaywallSource) {
  fetch(miniAppApiUrl("paywall-opened"), {
    method: "POST",
    headers: { ...miniAppHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
    keepalive: true,
  }).catch(() => undefined)
}

/** Платёж по пакету: обычная страница ЮKassa (redirect), как у бота. Цену проверяет сервер.
 *  С `method` — кнопка способа Т-Банка: кошелёк даёт ссылку (`app_link`), карта — форму банка для рамки шторки. */
export async function createPackPayment(
  tier: PackageTier,
  context: string,
  source: PaywallSource,
  method?: PayMethod,
): Promise<PayCreated> {
  let response: Response
  try {
    response = await fetch(miniAppApiUrl("payments/yookassa/create"), {
      method: "POST",
      cache: "no-store",
      headers: { ...miniAppHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({
        generations: tier.generations,
        amount_rubles: tier.rubles,
        context,
        method: method ?? "redirect",
        // Телефон: диплинк в приложение банка; компьютер: страница способа.
        flow: method && isMobileDevice() ? "app" : "web",
        // Форму оплаты можно показать внутри приложения: бэкенд ответит `embed: true`, если провайдер это умеет.
        // Кнопки кошельков и «другие способы» открываются снаружи — рамка только у карты.
        embed: method === undefined || method === "bank_card",
        // Без кнопок Т-Банк открывается в этом же окне приложения (старый путь, TBANK_TOP_LEVEL) и вернёт на эту страницу.
        ...(method ? {} : { return_to: `${window.location.origin}${window.location.pathname}` }),
        source,
      }),
    })
  } catch {
    throw new PayError("Нет связи. Проверь интернет и попробуй ещё раз.")
  }
  if (response.ok) return (await response.json()) as PayCreated
  if (response.status === 400) throw new PayError("Цены обновились. Выбери пакет ещё раз.")
  if (response.status === 409) throw new PayMethodUnavailable("Этот способ сейчас недоступен.")
  if (response.status === 401 || response.status === 403) {
    throw new PayError("Открой YOUR_BRAND из бота — так мы узнаем, кому зачислить энергию.")
  }
  if (response.status === 429) {
    throw new PayError("Слишком много неоплаченных попыток. Подожди пару минут и открой экран снова.")
  }
  throw new PayError("Не удалось создать платёж. Попробуй ещё раз.")
}

/** Билет оплаты кошельков Т-Банка: клиент берёт его заранее, а тап по кнопке способа открывает `pay-go` сразу, без `await`
 *  (после ожидания ответа жест пользователя потерян, и MAX / браузер молча не открывают ссылку). Платежа билет не создаёт. */
export async function createPayTicket(tier: PackageTier, context: string, source: PaywallSource): Promise<string> {
  let response: Response
  try {
    response = await fetch(miniAppApiUrl("payments/ticket"), {
      method: "POST",
      cache: "no-store",
      headers: { ...miniAppHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ generations: tier.generations, amount_rubles: tier.rubles, context, source }),
    })
  } catch {
    throw new PayError("Нет связи. Проверь интернет и попробуй ещё раз.")
  }
  if (response.ok) return ((await response.json()) as { ticket: string }).ticket
  if (response.status === 400) throw new PayError("Цены обновились. Выбери пакет ещё раз.")
  if (response.status === 401 || response.status === 403) {
    throw new PayError("Открой YOUR_BRAND из бота — так мы узнаем, кому зачислить энергию.")
  }
  throw new PayError("Не удалось подготовить оплату. Попробуй ещё раз.")
}

/* ------------------ неоплаченный платёж переживает перезапуск ---------------- */

/** Страница оплаты открывается в браузере, а возврат в приложение может поднять его заново — тогда
 *  память страницы пуста. Неоплаченный платёж кладём в хранилище и по возврату продолжаем опрос. */
export type PendingPayment = {
  id: string
  generations: number
  amount: number
  url: string
  at: number
  /** Платёж Т-Банка (форма внутри приложения или в этом же окне): после ухода и возврата проверяем сами, без кнопок. */
  embed?: boolean
  quiet?: boolean
  /** Только в памяти: банк при возврате сказал «отказ». */
  bankFailed?: boolean
}

const PENDING_KEY = "app-pay-pending"
const PENDING_TTL_MS = 40 * 60_000

export function rememberPending(payment: PayCreated) {
  const value: PendingPayment = {
    id: payment.payment_id,
    generations: payment.generations,
    amount: payment.amount,
    url: payment.confirmation_url ?? "",
    at: Date.now(),
    // Платёж Т-Банка (кошелёк, карта, страница банка): после возврата проверяем сами, без кнопок «Я оплатил».
    embed: payment.provider === "tbank" || payment.embed === true || payment.top_level === true,
  }
  try {
    window.localStorage.setItem(PENDING_KEY, JSON.stringify(value))
  } catch {
    // Хранилище закрыто (приватный режим) — вернёмся без него, по кнопке «Я оплатил».
  }
}

/** Ушли оплачивать по билету (`pay-go`): платёж создаст страница, поэтому помним билет — при перезапуске приложение опросит его
 *  статус (`id` = билет, статус билета понимает `fetchPaymentStatus`). */
export function rememberPendingTicket(ticket: string, tier: PackageTier) {
  const value: PendingPayment = { id: ticket, generations: tier.generations, amount: tier.rubles, url: "", at: Date.now(), embed: true }
  try {
    window.localStorage.setItem(PENDING_KEY, JSON.stringify(value))
  } catch {
    // Хранилище закрыто (приватный режим) — вернёмся без него.
  }
}

/** Платёж не дождались и вернули выбор пакета: ещё может пройти, зачислим при возврате, но шторку сами не поднимаем. */
export function markPendingQuiet() {
  const pending = readPending()
  if (!pending) return
  try {
    window.localStorage.setItem(PENDING_KEY, JSON.stringify({ ...pending, quiet: true }))
  } catch {
    // Хранилище закрыто — не страшно.
  }
}

export function forgetPending() {
  try {
    window.localStorage.removeItem(PENDING_KEY)
  } catch {
    // Нечего чистить.
  }
}

export function readPending(): PendingPayment | null {
  try {
    const raw = window.localStorage.getItem(PENDING_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as PendingPayment
    if (!value?.id || Date.now() - value.at > PENDING_TTL_MS) {
      forgetPending()
      return null
    }
    return value
  } catch {
    return null
  }
}

/** Банк вернул на страницу, с которой мы ушли в его форму: `?pay=return&ok=1|0` (их ставит страница `pay-return`).
 *  Параметры убираем из адреса, чтобы перезагрузка не повторила возврат. */
export function consumePayReturn(): "ok" | "fail" | null {
  try {
    const url = new URL(window.location.href)
    if (url.searchParams.get("pay") !== "return") return null
    const result = url.searchParams.get("ok") === "0" ? "fail" : "ok"
    url.searchParams.delete("pay")
    url.searchParams.delete("ok")
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash)
    return result
  } catch {
    return null
  }
}

/** При запуске: платёж, который был открыт до ухода в банк. Оплачен — зачисляем и говорим;
 *  ждёт — открываем экран ожидания (кроме платежа, который сам закрыли без оплаты, но не при возврате из банка);
 *  отменён или забыт — молча чистим (после возврата из банка с отказом — плашка «деньги не списаны»). */
export async function resumePendingPayment(onPaid: (status: PayStatus) => void) {
  const returned = consumePayReturn()
  const pending = readPending()
  if (!pending) return
  try {
    const status = await fetchPaymentStatus(pending.id)
    if (status.status === "succeeded") {
      forgetPending()
      onPaid(status)
    } else if (status.status === "canceled") {
      forgetPending()
      if (returned) openPaywall("resume", null, "declined")
    } else if (returned || !pending.quiet) {
      openPaywall("resume", { ...pending, bankFailed: returned === "fail" })
    }
  } catch {
    // Сеть моргнула: запись останется до конца срока, платёж ничего не теряет.
  }
}
