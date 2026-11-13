import { Fragment, type ReactNode } from "react"

/**
 * Внутренняя валюта — «энергия». Молния тёплого янтаря (--energy) с тонким
 * тёмным контуром (--energy-edge): на светлой плашке читается по контуру, на
 * тёмной — по заливке. Токены и их контраст — styles/tokens.css, check-contrast.
 */
export function EnergyBolt({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      className={className ?? "size-[1em]"}
      focusable="false"
      viewBox="0 0 24 24"
    >
      <path
        d="M13.6 1.8 4.6 13.4h5.9l-1.3 8.8 9.4-11.9h-5.9z"
        fill="var(--energy)"
        stroke="var(--energy-edge)"
        strokeLinejoin="round"
        strokeWidth="1.5"
      />
    </svg>
  )
}

/** Молния на фиолетовой монете: янтарь на --energy-plate держит ≥3:1 в обеих темах. */
export function EnergyCoin({ className, boltClassName }: { className?: string; boltClassName?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`grid shrink-0 place-items-center rounded-full border-(length:--outline-w) border-outline bg-energy-plate ${className ?? "size-7"}`}
    >
      <EnergyBolt className={boltClassName ?? "size-[65%]"} />
    </span>
  )
}

/** Текст с «⚡» (подписи из бэкенда): эмодзи заменяем на нашу молнию. */
export function EnergyText({ children }: { children: string | undefined }): ReactNode {
  if (!children) return null
  const parts = children.split("⚡")
  return parts.map((part, index) => (
    <Fragment key={index}>
      {index > 0 ? <EnergyBolt className="inline-block size-[1.05em] translate-y-[0.1em]" /> : null}
      {part}
    </Fragment>
  ))
}
