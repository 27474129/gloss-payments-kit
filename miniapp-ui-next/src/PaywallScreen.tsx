import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { BottomSheet, SheetCloseButton } from "@/components/BottomSheet"
import { EnergyBolt, EnergyCoin } from "@/components/Energy"
import { Button } from "@/components/ui/button"
import { showMiniAppToast } from "@/components/ui/sonner"
import { fetchPaymentStatus, isMobileDevice, PayError, PayMethodUnavailable, payGoUrl, WALLET_WAY, type PayCreated, type PayMethod, type PayStatus } from "@/lib/pay"
import {
  announceBalance,
  closePaywall,
  createPackPayment,
  createPayTicket,
  PAY_RETURN_MESSAGE,
  loadPackages,
  forgetPending,
  markPendingQuiet,
  paywallUnavailable,
  rememberPending,
  rememberPendingTicket,
  trackPaywallOpened,
  type PackageTier,
  type Packages,
  type PaywallNotice,
  type PaywallSource,
  type PendingPayment,
  type SpecialOffer,
} from "@/lib/paywall"
import { cn } from "@/lib/utils"
import { BOT_START, openBotDeepLink, openExternalLink } from "@/platform/bridge"
import { PayButton } from "@/components/PayButtons"

/* Тексты — черновики, правит владелец. */
const TEXT = {
  title: "Пополнить энергию",
  haveEnergy: "у тебя",
  outOfEnergy: "Энергия закончилась",
  perFrame: "за кадр",
  popular: "🔥 Выбор большинства",
  offerBadge: "Спецпредложение",
  offerLeft: "ещё",
  gift: "в подарок",
  pay: (rubles: number) => `Оплатить ${rubles} ₽`,
  payNote: "СБП · SberPay · T-Pay · карта — через ЮKassa",
  payNoteBank: "SberPay · СБП · T-Pay · карта — через Т-Банк",
  testNote: "Тестовая оплата: деньги не списываются",
  ticketFailed: "Не удалось подготовить оплату",
  ticketRetry: "Повторить",
  methodSbp: "СБП",
  methodSberpay: "SberPay",
  methodTpay: "T-Pay",
  methodCard: "Картой",
  methodBankPage: "СБП и другие способы",
  payNoteButtons: (methods: string[]) => `${methods.join(" · ")} — через Т-Банк`,
  methodGone: "Этот способ сейчас недоступен — выбери другой",
  frameTitle: "Оплата картой",
  frameBack: "Назад",
  frameLoading: "Форма оплаты Т-Банка",
  checkingTitle: "Проверяем оплату…",
  checkingBody: "Это займёт несколько секунд — энергия начислится сама.",
  checkingBodyBrowser: "Оплати на открывшейся странице и вернись сюда — энергия начислится сама.",
  checkingBodyApp: "Оплати в открывшемся приложении банка и вернись сюда — энергия начислится сама.",
  checkingCancel: "Отменить",
  noticeDeclined: "Оплата не завершилась — деньги не списаны",
  noticeUnfinished: "Оплата не завершена",
  creating: "Готовим оплату…",
  waitingTitle: "Ждём оплату",
  waitingBody: "Оплати на открывшейся странице и вернись в приложение — энергия начислится сама.",
  check: "Я оплатил — проверить",
  reopen: "Открыть страницу оплаты ещё раз",
  another: "Выбрать другой пакет",
  notYet: "Оплату пока не видим. Если уже заплатил — подожди минуту и проверь снова.",
  canceled: "Платёж отменён. Деньги не списаны — выбери пакет ещё раз.",
  late: "Платёж обрабатывается дольше обычного. Энергия придёт сама, как только банк подтвердит оплату, — обычно это пара минут.",
  paidTitle: "Энергия начислена",
  retry: "Попробовать снова",
  inBot: "Выбрать пакет в боте",
  close: "Закрыть",
  loading: "Загружаем пакеты",
  packs: "Пакеты энергии",
}

const WAIT_INTERVAL_MS = 3000
const WAIT_DEADLINE_MS = 10 * 60_000
/** После закрытия формы банка: проверяем часто и недолго, потом возвращаем выбор пакета. */
const CHECK_INTERVAL_MS = 1500
const CHECK_DEADLINE_MS = 8000
/** Неоплаченный платёж, который ушёл с экрана, досматриваем в фоне, пока шторка открыта. */
const LATE_INTERVAL_MS = 5000

/** Почему вернулись к выбору пакета: банк отказал / просто не оплатили. */
type Notice = PaywallNotice
/** Страница банка открывается в этом же окне; если за это время окно так и не ушло, открываем её в браузере. */
const NAVIGATE_FALLBACK_MS = 4000

type Phase =
  | { name: "loading" }
  | { name: "choose"; packages: Packages }
  | { name: "creating"; packages: Packages; key: string; method: PayMethod | null }
  | {
      name: "waiting"
      payment: Pick<PayCreated, "payment_id" | "generations" | "amount"> & { url: string }
      note: string
      expired: boolean
      /** `classic` — ЮKassa: страница в браузере и кнопки; остальное — Т-Банк внутри приложения:
       *  `form` — форма открыта во весь экран, `checking` — форму закрыли, часто проверяем (до ~8 с),
       *  `browser` — ушли оплачивать в браузер, ждём до 10 мин. */
      stage: "classic" | "form" | "checking" | "browser"
      /** Страница возврата банка сказала «отказ». */
      failHint: boolean
      /** Ушли оплачивать в приложение банка (ссылка способа), а не на страницу. */
      app?: boolean
      /** Список пакетов, к которому вернёмся; null — шторку подняли после перезапуска. */
      packages: Packages | null
    }
  | { name: "failed"; message: string }

type Row = {
  key: string
  tier: PackageTier
  context: string
  /** Строка спецпредложения (с таймером), а не обычного меню. */
  special: boolean
}

const rowKey = (context: string, generations: number) => `${context}:${generations}`

/** Цена кадра в целых рублях. */
const perFrame = (tier: PackageTier) => Math.round(tier.rubles / Math.max(1, tier.generations))

/** Экономия против самого маленького пакета меню (10 ⚡), в процентах; 0 — не показываем. */
function savingPercent(tier: PackageTier, base: PackageTier | undefined) {
  if (!base || tier === base) return 0
  const baseFrame = base.rubles / Math.max(1, base.generations)
  if (baseFrame <= 0) return 0
  return Math.max(0, Math.round((1 - tier.rubles / Math.max(1, tier.generations) / baseFrame) * 100))
}

function clock(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000))
  const pad = (value: number) => String(value).padStart(2, "0")
  return `${Math.floor(total / 3600)}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`
}

/** Текущее время, тикает раз в секунду, пока `active`. */
function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  return now
}

function Badge({ children, tone }: { children: React.ReactNode; tone: "brand" | "soft" | "accent" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-[3px] text-caption leading-none font-bold whitespace-nowrap",
        tone === "brand" && "bg-brand text-brand-ink",
        tone === "accent" && "bg-accent-2 text-accent-2-ink",
        tone === "soft" && "border-(length:--outline-w) border-outline bg-surface-2 text-text",
      )}
    >
      {children}
    </span>
  )
}

function TierRow({
  row,
  selected,
  busy,
  disabled,
  saving,
  popular,
  left,
  onPick,
}: {
  row: Row
  selected: boolean
  busy: boolean
  disabled: boolean
  saving: number
  popular: boolean
  /** Осталось до конца спецпредложения (мс), если оно у строки есть. */
  left: number | null
  onPick: () => void
}) {
  const { tier } = row
  return (
    <button
      aria-checked={selected}
      className={cn(
        "chunk-press flex min-h-[3.25rem] w-full items-center gap-3 rounded-[var(--r-md)] border-(length:--outline-w) px-3 py-1.5 text-left text-text outline-none focus-visible:ring-3 focus-visible:ring-ring disabled:opacity-60",
        selected
          ? "border-[var(--selection-line)] bg-surface-2 shadow-[var(--chunk-shadow-sm)]"
          : "border-line bg-transparent",
      )}
      disabled={disabled}
      onClick={onPick}
      role="radio"
      type="button"
    >
      <span
        aria-hidden="true"
        className={cn(
          "grid size-5 shrink-0 place-items-center rounded-full border-(length:--outline-w)",
          selected ? "border-outline bg-brand" : "border-line-strong",
        )}
      >
        {selected ? <span className="size-2 rounded-full bg-brand-ink" /> : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="inline-flex items-center gap-1 font-display text-[1.0625rem] leading-none font-bold whitespace-nowrap">
            {tier.generations}
            <EnergyBolt className="size-[1.05em]" />
          </span>
          {row.special ? <Badge tone="accent">{TEXT.offerBadge}</Badge> : null}
          {popular ? <span className="pay-popular">{TEXT.popular}</span> : null}
        </span>
        <span className="mt-0.5 block text-caption leading-tight text-text-muted">
          {busy ? (
            TEXT.creating
          ) : (
            <>
              {perFrame(tier)} ₽ {TEXT.perFrame}
              {saving > 0 ? <span className="font-semibold text-text"> · −{saving} %</span> : null}
              {tier.bonus_generations > 0 ? (
                <>
                  {" · "}
                  <span className="font-semibold text-text">
                    +{tier.bonus_generations} <EnergyBolt className="inline-block size-[1.05em] translate-y-[0.1em]" /> {TEXT.gift}
                  </span>
                </>
              ) : null}
              {left != null ? (
                <>
                  {" · "}
                  <span className="font-semibold text-text tabular-nums">
                    {TEXT.offerLeft} {clock(left)}
                  </span>
                </>
              ) : null}
            </>
          )}
        </span>
      </span>
      <span className="shrink-0 text-right">
        <span className="block font-display text-[1.0625rem] leading-none font-bold whitespace-nowrap">{tier.rubles} ₽</span>
        {tier.original_rubles > tier.rubles ? (
          <span className="mt-0.5 block text-caption leading-none whitespace-nowrap text-text-muted line-through">{tier.original_rubles} ₽</span>
        ) : null}
      </span>
    </button>
  )
}

/** «Пополнить энергию» внутри приложения: шторка снизу со списком пакетов как в меню бота → платёж ЮKassa
 *  (страница оплаты открывается в браузере клиента) → ожидание и начисление. Поверх оболочки, её состояние
 *  не теряется. `balance` — для шапки. Сколько ⚡ не хватает на образ, клиент не знает (цену образа и 402 бэкенд
 *  не отдаёт), поэтому из тоста «недостаточно энергии» показываем только «Энергия закончилась». */
export default function PaywallScreen({
  source,
  resume,
  notice: initialNotice,
  balance,
}: {
  source: PaywallSource
  resume: PendingPayment | null
  /** Вернулись из банка без оплаты. */
  notice: Notice | null
  balance: number | null
}) {
  const [phase, setPhase] = useState<Phase>(
    resume
      ? {
          name: "waiting",
          payment: { payment_id: resume.id, generations: resume.generations, amount: resume.amount, url: resume.url },
          note: "",
          expired: false,
          stage: resume.embed === true ? "checking" : "classic",
          failHint: resume.bankFailed === true,
          packages: null,
        }
      : { name: "loading" },
  )
  const [retry, setRetry] = useState(0)
  /** Плашка над списком пакетов после неудачной оплаты. */
  const [notice, setNotice] = useState<Notice | null>(initialNotice)
  /** Способы, на которые банк отказал в ссылке за эту сессию: кнопок нет. */
  const [gone, setGone] = useState<ReadonlySet<PayMethod>>(() => new Set())
  /** Плашка про недоступный способ. */
  const [methodNote, setMethodNote] = useState<string | null>(null)
  /** Платёж, который не дождались, но он ещё может пройти (СБП в другом приложении): досматриваем в фоне. */
  const [lateId, setLateId] = useState<string | null>(null)
  /** Номер последней попытки: ответ устаревшей (человек нажал «Назад») игнорируем. */
  const attemptRef = useRef(0)
  /** Билет оплаты для кнопок способов: берётся при открытии шторки и при смене пакета (см. `createPayTicket`). */
  const [ticket, setTicket] = useState<
    { key: string; state: "loading" } | { key: string; state: "ready"; value: string } | { key: string; state: "error" } | null
  >(null)
  const ticketAttemptRef = useRef(0)
  /** Текущая фаза для таймеров, которые не должны перезапускаться при каждой смене. */
  const phaseRef = useRef(phase)
  phaseRef.current = phase

  // Пакеты: из предзагрузки при старте (до 2 мин), иначе свежий запрос.
  const needPackages = phase.name === "loading"
  useEffect(() => {
    if (!needPackages) return
    let cancelled = false
    loadPackages()
      .then((packages) => {
        if (cancelled) return
        if (!packages.enabled) return paywallUnavailable()
        if (!packages.tiers.length && !packages.offer) {
          return setPhase({ name: "failed", message: "Сейчас нет доступных пакетов. Попробуй позже." })
        }
        trackPaywallOpened(source)
        setPhase({ name: "choose", packages })
      })
      .catch((reason: unknown) => {
        if (cancelled) return
        setPhase({ name: "failed", message: reason instanceof PayError ? reason.message : "Не удалось загрузить пакеты. Попробуй ещё раз." })
      })
    return () => {
      cancelled = true
    }
  }, [needPackages, retry, source])

  const paid = useCallback((status: PayStatus) => {
    forgetPending()
    if (status.balance != null) announceBalance(status.balance)
    showMiniAppToast({
      variant: "success",
      title: TEXT.paidTitle,
      description: `+${status.credited_generations ?? status.generations} ⚡${status.balance != null ? ` · на балансе ${status.balance}` : ""}`,
      duration: 5_000,
    })
    closePaywall()
  }, [])

  /** Платёж Т-Банка: форма открывается в этом же окне приложения (в iframe у банка только карта, без СБП и T-Pay), банк
   *  вернёт на эту страницу с `?pay=return`, и приложение само проверит платёж. Шторка пока остаётся в «Готовим оплату…». */
  const bankRef = useRef<{ timer: number; id: string; packages: Packages } | null>(null)
  const goToBank = useCallback((payment: PayCreated, url: string, packages: Packages, attempt: number) => {
    const id = payment.payment_id
    const timer = window.setTimeout(() => {
      // Окно так и не ушло (клиент не пускает на чужой адрес): запасной путь — браузер, ждём там до 10 мин.
      bankRef.current = null
      if (attempt !== attemptRef.current) return
      openExternalLink(url)
      setPhase((current) =>
        current.name === "creating"
          ? { name: "waiting", payment: { payment_id: id, generations: payment.generations, amount: payment.amount, url }, note: "", expired: false, stage: "browser", failHint: false, packages }
          : current,
      )
    }, NAVIGATE_FALLBACK_MS)
    bankRef.current = { timer, id, packages }
    window.location.assign(url)
  }, [])

  // Назад из банка кнопкой клиента (страница вернулась из кэша, не перезагрузилась) или окно ушло в фон: не открываем браузер сами.
  useEffect(() => {
    const cancelFallback = () => {
      if (bankRef.current) window.clearTimeout(bankRef.current.timer)
    }
    const onPageShow = (event: PageTransitionEvent) => {
      const bank = bankRef.current
      if (!event.persisted || !bank) return
      bankRef.current = null
      window.clearTimeout(bank.timer)
      markPendingQuiet()
      setLateId(bank.id)
      setNotice("unfinished")
      setPhase({ name: "choose", packages: bank.packages })
    }
    window.addEventListener("pagehide", cancelFallback)
    window.addEventListener("pageshow", onPageShow)
    return () => {
      window.removeEventListener("pagehide", cancelFallback)
      window.removeEventListener("pageshow", onPageShow)
      cancelFallback()
    }
  }, [])

  /** `method` — кнопка способа Т-Банка: кошелёк (`app_link`) открывается снаружи, карта — во фрейме шторки, «другие способы» —
   *  страница банка снаружи. Без него — единственная кнопка «Оплатить» (ЮKassa и старый путь Т-Банка). */
  const pick = useCallback(
    (tier: PackageTier, context: string, packages: Packages, method?: PayMethod) => {
      const attempt = ++attemptRef.current
      setNotice(null)
      setMethodNote(null)
      setPhase({ name: "creating", packages, key: `${context}:${tier.generations}`, method: method ?? null })
      createPackPayment(tier, context, source, method)
        .then((payment) => {
          if (attempt !== attemptRef.current) return
          if (!payment.confirmation_url) throw new PayError("Не удалось создать платёж. Попробуй ещё раз.")
          rememberPending(payment)
          if (payment.top_level) return goToBank(payment, payment.confirmation_url, packages, attempt)
          const embed = payment.embed === true
          const wallet = payment.confirmation_type === "app_link"
          setPhase({
            name: "waiting",
            payment: { payment_id: payment.payment_id, generations: payment.generations, amount: payment.amount, url: payment.confirmation_url },
            note: "",
            expired: false,
            // Кнопки Т-Банка: после ссылки шторка в «Проверяем оплату…» (опрос до 10 мин, «Отменить»); без них ЮKassa — «Ждём оплату».
            stage: embed ? "form" : method ? "browser" : "classic",
            failHint: false,
            app: wallet,
            packages,
          })
          if (!embed) openExternalLink(payment.confirmation_url)
        })
        .catch((reason: unknown) => {
          if (attempt !== attemptRef.current) return
          if (reason instanceof PayMethodUnavailable && method) {
            // Банк не подключил способ: кнопку убираем, остальные остаются.
            setGone((current) => new Set(current).add(method))
            setMethodNote(TEXT.methodGone)
            return setPhase({ name: "choose", packages })
          }
          setPhase({ name: "failed", message: reason instanceof PayError ? reason.message : "Не удалось создать платёж. Попробуй ещё раз." })
        })
    },
    [source, goToBank],
  )

  /** Билет на пакет живёт 10 минут: берём при открытии, при смене пакета и заново, пока человек думает. */
  const requestTicket = useCallback(
    (row: Row) => {
      const attempt = ++ticketAttemptRef.current
      setTicket({ key: row.key, state: "loading" })
      createPayTicket(row.tier, row.context, source)
        .then((value) => {
          if (attempt === ticketAttemptRef.current) setTicket({ key: row.key, state: "ready", value })
        })
        .catch(() => {
          if (attempt === ticketAttemptRef.current) setTicket({ key: row.key, state: "error" })
        })
    },
    [source],
  )

  /** Тап по SberPay / СБП / T-Pay / «странице банка»: ссылку открываем СРАЗУ, в обработчике тапа, до любого `await`, —
   *  пока у клиента есть жест пользователя. Платёж создаст страница `pay-go` по билету; шторка сразу ждёт его по билету. */
  const tapWallet = useCallback((method: PayMethod, row: Row, packages: Packages, current: typeof ticket) => {
    const way = WALLET_WAY[method]
    if (!way || current?.state !== "ready" || current.key !== row.key) return
    const url = payGoUrl(current.value, way)
    openExternalLink(url)
    rememberPendingTicket(current.value, row.tier)
    attemptRef.current += 1
    ticketAttemptRef.current += 1
    setTicket(null)
    setNotice(null)
    setPhase({
      name: "waiting",
      payment: { payment_id: current.value, generations: row.tier.generations, amount: row.tier.rubles, url },
      note: "",
      expired: false,
      stage: "browser",
      failHint: false,
      app: method === "sberbank",
      packages,
    })
  }, [])

  /** Закрыли сами — больше не напоминаем про этот платёж при следующем запуске. */
  const close = useCallback(() => {
    forgetPending()
    closePaywall()
  }, [])

  /** Шторку закрыли жестом, крестиком или тапом по затемнению. Платёж в ожидании не теряем: он остаётся
   *  в хранилище, и при следующем запуске приложение вернёт экран ожидания. */
  const dismiss = useCallback(() => {
    if (phase.name !== "waiting" && !lateId) forgetPending()
    closePaywall()
  }, [phase.name, lateId])

  const back = useCallback(() => {
    attemptRef.current += 1
    setPhase({ name: "loading" })
  }, [])

  /** Вернуться к обычному выбору пакета с плашкой. Отказ банка — платёж закрыт; «просто не оплатили» —
   *  платёж остаётся в хранилище и в фоне (пока шторка открыта и при следующем запуске) может ещё зачислиться. */
  const leave = useCallback((kind: Notice) => {
    const current = phaseRef.current
    if (current.name !== "waiting") return
    if (kind === "declined") {
      forgetPending()
      setLateId(null)
    } else {
      markPendingQuiet()
      setLateId(current.payment.payment_id)
    }
    setNotice(kind)
    setPhase(current.packages ? { name: "choose", packages: current.packages } : { name: "loading" })
  }, [])

  // Ждём оплату: опрос статуса, и сразу же при возврате в приложение.
  const waitingId = phase.name === "waiting" && !phase.expired ? phase.payment.payment_id : null
  const waitingStage = phase.name === "waiting" ? phase.stage : null
  useEffect(() => {
    if (!waitingId || !waitingStage) return
    const quick = waitingStage === "checking"
    const interval = quick ? CHECK_INTERVAL_MS : WAIT_INTERVAL_MS
    const deadline = quick ? CHECK_DEADLINE_MS : WAIT_DEADLINE_MS
    let stopped = false
    const startedAt = Date.now()
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      try {
        const status = await fetchPaymentStatus(waitingId)
        if (stopped) return
        if (status.status === "succeeded") return paid(status)
        // Банк пишет «отказ» сразу, но на той же форме можно заплатить другой картой: пока форма открыта, не закрываем её.
        if (status.status === "canceled" && waitingStage !== "form") {
          if (waitingStage === "classic") {
            forgetPending()
            return setPhase({ name: "failed", message: TEXT.canceled })
          }
          return leave("declined")
        }
      } catch {
        // Сеть моргнула — следующий опрос повторит.
      }
      if (stopped) return
      const elapsed = Date.now() - startedAt
      if (elapsed >= deadline) {
        if (quick) {
          const current = phaseRef.current
          return leave(current.name === "waiting" && current.failHint ? "declined" : "unfinished")
        }
        return setPhase((current) => (current.name === "waiting" ? { ...current, expired: true } : current))
      }
      timer = setTimeout(tick, Math.min(interval, deadline - elapsed))
    }
    // После закрытия формы первая проверка сразу, не через интервал.
    timer = setTimeout(tick, quick ? 0 : interval)
    const onVisible = () => {
      if (document.visibilityState !== "visible" || stopped) return
      clearTimeout(timer)
      timer = setTimeout(tick, 0)
    }
    document.addEventListener("visibilitychange", onVisible)
    return () => {
      stopped = true
      clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisible)
    }
  }, [waitingId, waitingStage, paid, leave])

  // Фон: «не оплатили» за 8 с, но платёж мог дойти позже. Пока видна не ожидающая оплаты шторка, досматриваем его.
  const lateWatch = lateId !== null && phase.name !== "waiting" && phase.name !== "creating"
  useEffect(() => {
    if (!lateWatch || !lateId) return
    let stopped = false
    const startedAt = Date.now()
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      try {
        const status = await fetchPaymentStatus(lateId)
        if (stopped) return
        if (status.status === "succeeded") return paid(status)
        if (status.status === "canceled") return setLateId(null)
      } catch {
        // Сеть моргнула — следующий опрос повторит.
      }
      if (stopped) return
      if (Date.now() - startedAt > WAIT_DEADLINE_MS) return setLateId(null)
      timer = setTimeout(tick, LATE_INTERVAL_MS)
    }
    timer = setTimeout(tick, LATE_INTERVAL_MS)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [lateWatch, lateId, paid])

  const checkNow = useCallback(
    async (id: string) => {
      let note = TEXT.notYet
      try {
        const status = await fetchPaymentStatus(id)
        if (status.status === "succeeded") return paid(status)
        if (status.status === "canceled") {
          forgetPending()
          return setPhase({ name: "failed", message: TEXT.canceled })
        }
      } catch {
        note = "Не получилось проверить: нет связи. Попробуй ещё раз."
      }
      setPhase((current) => (current.name === "waiting" ? { ...current, note } : current))
    },
    [paid],
  )

  // Форма банка в iframe сообщает со страницы возврата (`pay-return`), что закончилась: закрываем её, шторка
  // сама проверяет платёж (в опросе выше). Сообщению не доверяем: статус всё равно читается с сервера.
  const frameRef = useRef<HTMLIFrameElement>(null)
  const formOpen = phase.name === "waiting" && phase.stage === "form"
  useEffect(() => {
    if (!formOpen) return
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: string; ok?: boolean } | null
      if (data?.type !== PAY_RETURN_MESSAGE) return
      // Слушаем только окно нашей формы.
      if (event.source !== frameRef.current?.contentWindow) return
      setPhase((current) =>
        current.name === "waiting" && current.stage === "form"
          ? { ...current, stage: "checking", failHint: data.ok === false, note: "" }
          : current,
      )
    }
    window.addEventListener("message", onMessage)
    return () => window.removeEventListener("message", onMessage)
  }, [formOpen])

  /** Крестик в шапке формы: закрыли форму сами — дальше «Проверяем оплату…». */
  const closeForm = useCallback(() => {
    setPhase((current) => (current.name === "waiting" && current.stage === "form" ? { ...current, stage: "checking" } : current))
  }, [])

  const packages = phase.name === "choose" || phase.name === "creating" ? phase.packages : null
  const creating = phase.name === "creating" ? phase.key : null

  const offerLive = packages?.offer ?? null
  const now = useNow(offerLive != null)
  const offerLeft = offerLive ? new Date(offerLive.expires_at).getTime() - now : null
  // Время вышло — спецпредложение прячем (сервер всё равно не примет), выбор вернётся к обычным пакетам.
  const offer: SpecialOffer | null = offerLive && !(offerLeft != null && offerLeft <= 0) ? offerLive : null

  const { rows, base } = useMemo(() => {
    const menu = [...(packages?.tiers ?? [])].sort((x, y) => x.generations - y.generations)
    const menuRows: Row[] = menu.map((tier) => ({ key: rowKey("menu", tier.generations), tier, context: "menu", special: false }))
    const offerRows: Row[] = (offer?.tiers ?? []).map((tier) => ({
      key: rowKey(offer!.context, tier.generations),
      tier,
      context: offer!.context,
      special: true,
    }))
    return { rows: [...offerRows, ...menuRows], base: menu[0] }
  }, [packages, offer])

  const [picked, setPicked] = useState<string | null>(null)
  const allRows = rows
  // По умолчанию: спецпредложение, иначе 10 ⚡ (как покупают сейчас), иначе первый пакет.
  const defaultKey = allRows.find((row) => row.special)?.key ?? allRows.find((row) => row.tier.generations === 10 && !row.special)?.key ?? allRows[0]?.key ?? null
  const selectedRow = rows.find((row) => row.key === picked) ?? allRows.find((row) => row.key === defaultKey) ?? null
  const selected = selectedRow?.key ?? null

  const statusLine = source === "toast" ? TEXT.outOfEnergy : null
  const sheetBusy = creating !== null
  /** Кнопки способов Т-Банка вместо одной «Оплатить» (ЮKassa и старый путь Т-Банка — без них). */
  const buttonsMode = packages !== null && packages.pay_methods != null && selectedRow !== null

  // Билет для кнопок: сразу при открытии, при смене пакета — через 250 мс (человек листает пакеты), пока билета нет, кнопки ждут.
  const ticketKey = buttonsMode && phase.name === "choose" ? selectedRow.key : null
  const ticketRef = useRef(ticket)
  ticketRef.current = ticket
  const selectedRowRef = useRef(selectedRow)
  selectedRowRef.current = selectedRow
  useEffect(() => {
    if (!ticketKey) return
    const row = selectedRowRef.current
    if (!row) return
    const timer = window.setTimeout(() => requestTicket(row), ticketRef.current ? 250 : 0)
    return () => {
      window.clearTimeout(timer)
      ticketAttemptRef.current += 1
    }
  }, [ticketKey, requestTicket])
  useEffect(() => {
    if (!ticketKey) setTicket(null)
  }, [ticketKey])

  // Билет живёт 10 минут: пока человек думает, берём новый заранее.
  const readyTicket = ticket?.state === "ready" ? ticket.value : null
  useEffect(() => {
    if (!readyTicket || !selectedRowRef.current) return
    const timer = window.setTimeout(() => {
      const row = selectedRowRef.current
      if (row) requestTicket(row)
    }, 8 * 60_000)
    return () => window.clearTimeout(timer)
  }, [readyTicket, requestTicket])

  const stage = phase.name === "waiting" ? phase.stage : null

  const header = (closeSheet: () => void) => (
    <div className="pb-3">
      <div className="flex items-center gap-3">
        <h2 className="min-w-0 flex-1 font-display text-[0.9375rem] leading-tight font-extrabold tracking-tight text-brand-text">
          {stage === "form" ? TEXT.frameTitle : TEXT.title}
        </h2>
        {balance != null ? (
          <span className="inline-flex shrink-0 items-center gap-1 text-label whitespace-nowrap text-text-muted">
            {TEXT.haveEnergy} <b className="font-bold text-text tabular-nums">{balance}</b>
            <EnergyBolt className="size-[1.1em]" />
          </span>
        ) : null}
        <SheetCloseButton onClick={stage === "form" ? closeForm : closeSheet} />
      </div>
      {statusLine ? (
        <p className="mt-1 text-label font-semibold text-text">{statusLine}</p>
      ) : null}
    </div>
  )

  // Кнопки способов Т-Банка: кошельки по порядку (СБП, SberPay, T-Pay), без отказанных банком; не подключён ни один —
  // «СБП и другие способы» (страница банка); карта всегда. SberPay с компьютера просит телефон — там его нет.
  const payMethods = packages?.pay_methods ?? null
  const methodButtons: { method: PayMethod; label: string }[] = []
  if (payMethods) {
    const wallets = payMethods.wallets.filter((method) => !gone.has(method) && (method !== "sberbank" || isMobileDevice()))
    const labels: Partial<Record<PayMethod, string>> = { sbp: TEXT.methodSbp, sberbank: TEXT.methodSberpay, tinkoff_bank: TEXT.methodTpay }
    for (const method of wallets) methodButtons.push({ method, label: labels[method] ?? method })
    if (!wallets.length) methodButtons.push({ method: "redirect", label: TEXT.methodBankPage })
    if (payMethods.card) methodButtons.push({ method: "bank_card", label: TEXT.methodCard })
  }
  // Подпись под кнопками перечисляет то, что на них есть, в том же порядке (на «СБП и другие способы» — СБП и T-Pay).
  const noteMethods = methodButtons.flatMap((item) =>
    item.method === "redirect" ? [TEXT.methodSbp, TEXT.methodTpay] : item.method === "bank_card" ? ["карта"] : [item.label],
  )
  const pressed = phase.name === "creating" ? phase.method : null

  let footer: React.ReactNode = null
  if (packages && selectedRow && payMethods) {
    // Один экран: SberPay (или первый кошелёк) — во всю ширину с суммой; под ней в один ряд остальные кошельки и «Картой».
    // Кошельки открывают ссылку прямо из тапа по билету (см. `tapWallet`) и ждут его; карте билет не нужен (форма во фрейме).
    const [main, ...others] = methodButtons
    const ticketReady = ticket?.state === "ready" && ticket.key === selectedRow.key
    const ticketError = ticket?.state === "error" && ticket.key === selectedRow.key
    const button = (item: { method: PayMethod; label: string }, size: "main" | "regular") => (
      <PayButton
        amount={selectedRow.tier.rubles}
        busyText={pressed === item.method ? TEXT.creating : undefined}
        disabled={sheetBusy || (item.method !== "bank_card" && !ticketReady)}
        key={item.method}
        kind={item.method}
        label={item.label}
        onClick={() =>
          item.method === "bank_card"
            ? pick(selectedRow.tier, selectedRow.context, packages, item.method)
            : tapWallet(item.method, selectedRow, packages, ticket)
        }
        pending={item.method !== "bank_card" && !ticketReady && !ticketError && !sheetBusy}
        size={size}
      />
    )
    footer = (
      <div>
        {main ? button(main, "main") : null}
        {others.length ? (
          <div className={cn("mt-2 grid gap-2", others.length === 1 ? "grid-cols-1" : others.length === 2 ? "grid-cols-2" : "grid-cols-3")}>
            {others.map((item) => button(item, "regular"))}
          </div>
        ) : null}
        {ticketError ? (
          <div className="mt-2 flex items-center gap-2 text-caption font-semibold text-text">
            <span className="min-w-0 flex-1">{TEXT.ticketFailed}</span>
            <Button onClick={() => requestTicket(selectedRow)} size="sm" variant="secondary">
              {TEXT.ticketRetry}
            </Button>
          </div>
        ) : (
          <p className="mt-1.5 text-center text-caption leading-snug font-medium text-text-muted">
            {packages.test_mode ? TEXT.testNote : TEXT.payNoteButtons(noteMethods)}
          </p>
        )}
      </div>
    )
  } else if (packages && selectedRow) {
    footer = (
      <>
        <Button
          className="w-full"
          disabled={sheetBusy}
          onClick={() => pick(selectedRow.tier, selectedRow.context, packages)}
          size="lg"
        >
          {sheetBusy ? TEXT.creating : TEXT.pay(selectedRow.tier.rubles)}
        </Button>
        <p className="mt-2 text-center text-caption leading-snug font-medium text-text-muted">
          {packages.test_mode ? TEXT.testNote : packages.provider === "tbank" ? TEXT.payNoteBank : TEXT.payNote}
        </p>
      </>
    )
  } else if (stage === "form" && phase.name === "waiting") {
    // Форма банка во фрейме (в ней только карта): свой «Назад» вместо выхода из формы банка. Платёж не теряем: шторка
    // ещё несколько секунд проверяет его и только потом возвращает список (как и крестик).
    footer = (
      <Button className="w-full" onClick={closeForm} variant="ghost">
        {TEXT.frameBack}
      </Button>
    )
  } else if (stage === "browser" && phase.name === "waiting") {
    const reopenUrl = phase.payment.url
    footer = (
      <div className="flex flex-col gap-2">
        {reopenUrl ? (
          <Button className="w-full" onClick={() => openExternalLink(reopenUrl)} variant="secondary">
            {TEXT.reopen}
          </Button>
        ) : null}
        <Button className="w-full" onClick={() => leave("unfinished")} variant="ghost">
          {TEXT.checkingCancel}
        </Button>
      </div>
    )
  } else if (phase.name === "waiting" && stage === "classic") {
    footer = (
      <div className="flex flex-col gap-2">
        <Button className="w-full" onClick={() => void checkNow(phase.payment.payment_id)} size="lg">
          {TEXT.check}
        </Button>
        {phase.payment.url ? (
          <Button className="w-full" onClick={() => openExternalLink(phase.payment.url)} variant="secondary">
            {TEXT.reopen}
          </Button>
        ) : null}
        <Button className="w-full" onClick={() => { forgetPending(); back() }} variant="ghost">
          {TEXT.another}
        </Button>
      </div>
    )
  } else if (phase.name === "failed") {
    footer = (
      <div className="flex flex-col gap-2">
        <Button className="w-full" onClick={() => { back(); setRetry((value) => value + 1) }} size="lg">
          {TEXT.retry}
        </Button>
        <Button className="w-full" onClick={() => { close(); openBotDeepLink(BOT_START.buyGenerations) }} variant="secondary">
          {TEXT.inBot}
        </Button>
        <Button className="w-full" onClick={close} variant="ghost">
          {TEXT.close}
        </Button>
      </div>
    )
  }

  return (
    <BottomSheet footer={footer} header={header} onClosed={dismiss} tall={stage === "form"} title={TEXT.title}>
      {phase.name === "loading" ? (
        <div aria-label={TEXT.loading} className="flex flex-col gap-2" role="status">
          {Array.from({ length: 5 }, (_, i) => (
            <div className="h-[3.25rem] animate-pulse rounded-[var(--r-md)] bg-surface-2" key={i} />
          ))}
        </div>
      ) : null}

      {packages && methodNote && !notice ? (
        <p
          aria-live="polite"
          className="mb-2 rounded-[var(--r-md)] border-(length:--outline-w) border-line bg-surface-2 px-3 py-2 text-label font-semibold text-text"
          role="status"
        >
          {methodNote}
        </p>
      ) : null}

      {packages && notice ? (
        <p
          aria-live="polite"
          className="mb-2 rounded-[var(--r-md)] border-(length:--outline-w) border-line bg-surface-2 px-3 py-2 text-label font-semibold text-text"
          role="status"
        >
          {notice === "declined" ? TEXT.noticeDeclined : TEXT.noticeUnfinished}
        </p>
      ) : null}

      {packages ? (
        <div aria-label={TEXT.packs} className="flex flex-col gap-1.5" role="radiogroup">
          {allRows.map((row) => (
            <TierRow
              popular={!row.special && row.tier.generations === 50}
              busy={creating === row.key}
              disabled={sheetBusy}
              key={row.key}
              left={row.special ? offerLeft : null}
              onPick={() => setPicked(row.key)}
              row={row}
              saving={row.special ? 0 : savingPercent(row.tier, base)}
              selected={row.key === selected}
            />
          ))}
        </div>
      ) : null}

      {phase.name === "waiting" && stage === "form" ? (
        <iframe
          allow="payment *; clipboard-write"
          className="block h-[calc(100svh-10.5rem)] min-h-[360px] w-full rounded-[var(--r-md)] border-0 bg-white"
          ref={frameRef}
          src={phase.payment.url}
          title={TEXT.frameLoading}
        />
      ) : null}

      {phase.name === "waiting" && stage !== "form" ? (
        <section aria-live="polite" className="flex flex-col gap-3 pt-1">
          <div
            className={cn(
              "flex items-center gap-3 rounded-[var(--r-md)] border-(length:--outline-w) border-line bg-surface-2 p-3",
              stage !== "classic" && !phase.expired && "animate-pulse motion-reduce:animate-none",
            )}
          >
            <EnergyCoin className="size-10" />
            <div className="min-w-0 flex-1">
              <p className="font-display text-[1.0625rem] leading-none font-bold text-text">
                {phase.payment.generations} <EnergyBolt className="inline-block size-[1.05em] translate-y-[0.1em]" />
              </p>
              <p className="mt-1 text-label text-text-muted">{phase.payment.amount} ₽</p>
            </div>
            {stage !== "classic" ? (
              <span
                aria-hidden="true"
                className="size-5 shrink-0 animate-spin rounded-full border-(length:--outline-w) border-line-strong border-t-brand motion-reduce:animate-none"
              />
            ) : null}
          </div>
          <p className="font-display text-title text-text">{stage === "classic" ? TEXT.waitingTitle : TEXT.checkingTitle}</p>
          <p className="text-body text-text-muted">
            {phase.expired ? TEXT.late : stage === "classic" ? TEXT.waitingBody : stage === "browser" ? (phase.app ? TEXT.checkingBodyApp : TEXT.checkingBodyBrowser) : TEXT.checkingBody}
          </p>
          {phase.note ? <p className="text-label text-text">{phase.note}</p> : null}
        </section>
      ) : null}

      {phase.name === "failed" ? (
        <section aria-live="assertive" className="pt-1">
          <p className="text-body text-text">{phase.message}</p>
        </section>
      ) : null}
    </BottomSheet>
  )
}
