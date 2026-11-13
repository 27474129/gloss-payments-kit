import { CreditCard } from "lucide-react"

import sberpayLogo from "@/assets/pay/sberpay.svg?raw"
import sbpLogo from "@/assets/pay/sbp.svg?raw"
import tpayLogo from "@/assets/pay/tpay.svg?raw"
import type { PayMethod } from "@/lib/pay"
import { cn } from "@/lib/utils"

/**
 * Кнопки способов оплаты Т-Банка. Платёжные кнопки делаются по руководствам банков и не перекрашиваются под тему:
 * SberPay — зелёная Сбера с шильдиком SberPay, T-Pay — жёлтая (#FFDD2D) с логотипом T-Pay, СБП — светлая со знаком НСПК,
 * «Картой» — нейтральная в стиле приложения. Логотипы лежат в `assets/pay/` (официальные SVG банков) и вставляются в
 * страницу как есть, без внешних загрузок.
 */
type Props = {
  kind: PayMethod
  /** Подпись способа: на кнопке без логотипа и для скринридера. */
  label: string
  /** Кнопка нажата — платёж создаётся: вместо логотипа короткий текст. */
  busyText?: string
  /** Сумма в рублях: SberPay пишет её на кнопке («Оплатить 299 ₽»). */
  amount?: number
  /** Ждём билет оплаты: кнопка неактивна, поверх неё спиннер. */
  pending?: boolean
  /** «main» — первая кнопка во всю ширину (48 px), иначе 44 px в ряд. */
  size?: "main" | "regular"
  disabled?: boolean
  onClick: () => void
  className?: string
}

const SBER_GREEN = "#21A038"
const TPAY_YELLOW = "#FFDD2D"
const TPAY_TEXT = "#1C1C1E"

function Logo({ svg, className }: { svg: string; className: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("inline-flex shrink-0 items-center [&>svg]:h-full [&>svg]:w-auto", className)}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

// Один вид на все кнопки: чанк-кнопка приложения (контур, нажатие вниз), а цвет и содержимое — банка.
const base =
  "chunk-press relative inline-flex w-full items-center justify-center gap-2 rounded-2xl border-(length:--outline-w) border-outline px-2 text-body font-bold whitespace-nowrap shadow-[var(--chunk-shadow-sm)] outline-none select-none focus-visible:ring-3 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-60 disabled:shadow-none"

export function PayButton({ kind, label, busyText, amount, pending, size = "regular", disabled, onClick, className }: Props) {
  const height = size === "main" ? "h-12" : "h-11"
  const logoHeight = size === "main" ? "h-7" : "h-6"
  let style: React.CSSProperties | undefined
  let tone = "bg-surface-1 text-text"
  let content: React.ReactNode = label

  if (busyText) {
    content = busyText
  }
  if (kind === "sberbank") {
    style = { backgroundColor: SBER_GREEN, color: "#fff" }
    tone = ""
    content = busyText ?? (
      <>
        {amount != null ? `Оплатить ${amount} ₽` : "Оплатить"}
        <Logo className={logoHeight} svg={sberpayLogo} />
      </>
    )
  } else if (kind === "tinkoff_bank") {
    style = { backgroundColor: TPAY_YELLOW, color: TPAY_TEXT }
    tone = ""
    content = busyText ?? <Logo className={size === "main" ? "h-6" : "h-5"} svg={tpayLogo} />
  } else if (kind === "sbp") {
    style = { backgroundColor: "#fff", color: "#1D1346" }
    tone = ""
    content = busyText ?? (
      <>
        <Logo className={logoHeight} svg={sbpLogo} />
        {label}
      </>
    )
  } else if (kind === "bank_card") {
    content = busyText ?? (
      <>
        <CreditCard className="size-5 shrink-0" />
        {label}
      </>
    )
  }

  return (
    <button
      aria-label={label}
      className={cn(base, height, tone, pending && "disabled:opacity-100", className)}
      disabled={disabled}
      onClick={onClick}
      style={style}
      type="button"
    >
      <span className={cn("inline-flex items-center justify-center gap-2", pending && "opacity-0")}>{content}</span>
      {pending ? (
        <span
          aria-hidden="true"
          className="absolute size-4 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none"
        />
      ) : null}
    </button>
  )
}
