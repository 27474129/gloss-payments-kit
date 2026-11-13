import { useCallback, useEffect, useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from "react"
import { Dialog as DialogPrimitive } from "radix-ui"
import { X } from "lucide-react"

import { Button } from "@/components/ui/button"

/**
 * Шторка снизу: затемнение, ручка, выезд снизу, закрытие свайпом вниз по шапке,
 * тапом по затемнению, крестиком и Esc. Высота по содержимому, не выше 88 % экрана;
 * прокручивается только середина, шапка и подвал приколоты.
 *
 * Радикс отдаёт ловушку фокуса и блокировку прокрутки страницы. Движение — через Web
 * Animations и прямой transform (без setState на каждый пиксель пальца).
 * При `prefers-reduced-motion` шторка появляется и исчезает без анимации.
 */

const OPEN_MS = 250
const CLOSE_MS = 200
const EASE_OUT = "cubic-bezier(0.2, 0.8, 0.2, 1)"
/** Сколько надо протянуть вниз, чтобы закрыть, и какая скорость (px/мс) считается броском. */
const DISMISS_DISTANCE = 90
const DISMISS_VELOCITY = 0.6

function reducedMotion() {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
}

export function BottomSheet({
  title,
  onClosed,
  header,
  footer,
  tall = false,
  children,
}: {
  /** Название для скринридера (видимый заголовок рисует `header`). */
  title: string
  /** Шторка закрылась (после анимации) — убрать её из дерева. */
  onClosed: () => void
  /** Шапка под ручкой: за неё же можно тянуть шторку вниз. Получает `close` для крестика. */
  header: (close: () => void) => ReactNode
  /** Липкий подвал (кнопка оплаты). */
  footer?: ReactNode
  /** На весь экран (форма оплаты банка внутри шторки). */
  tall?: boolean
  children: ReactNode
}) {
  const sheetRef = useRef<HTMLDivElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)
  const closingRef = useRef(false)
  const dragRef = useRef<{ id: number; startY: number; startT: number; dy: number } | null>(null)
  const onClosedRef = useRef(onClosed)
  onClosedRef.current = onClosed

  // Выезд снизу при монтировании.
  useEffect(() => {
    if (reducedMotion()) return
    sheetRef.current?.animate(
      [{ transform: "translateY(100%)" }, { transform: "translateY(0)" }],
      { duration: OPEN_MS, easing: EASE_OUT },
    )
    overlayRef.current?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: OPEN_MS, easing: "ease-out" })
  }, [])

  const close = useCallback((fromY = 0) => {
    if (closingRef.current) return
    closingRef.current = true
    const sheet = sheetRef.current
    if (!sheet || reducedMotion()) {
      onClosedRef.current()
      return
    }
    const distance = sheet.offsetHeight
    const slide = sheet.animate(
      [{ transform: `translateY(${fromY}px)` }, { transform: `translateY(${distance}px)` }],
      { duration: CLOSE_MS, easing: "ease-in", fill: "forwards" },
    )
    overlayRef.current?.animate([{ opacity: overlayRef.current ? Number(overlayRef.current.style.opacity || 1) : 1 }, { opacity: 0 }], {
      duration: CLOSE_MS,
      easing: "ease-in",
      fill: "forwards",
    })
    slide.onfinish = () => onClosedRef.current()
    slide.oncancel = () => onClosedRef.current()
  }, [])

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (closingRef.current || (event.pointerType === "mouse" && event.button !== 0)) return
    // Кнопки в шапке (крестик) остаются кнопками, а не началом жеста.
    if ((event.target as HTMLElement).closest("button, a")) return
    dragRef.current = { id: event.pointerId, startY: event.clientY, startT: event.timeStamp, dy: 0 }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.id !== event.pointerId) return
    drag.dy = Math.max(0, event.clientY - drag.startY)
    if (sheetRef.current) sheetRef.current.style.transform = `translateY(${drag.dy}px)`
    if (overlayRef.current) {
      overlayRef.current.style.opacity = String(Math.max(0.2, 1 - drag.dy / Math.max(1, sheetRef.current?.offsetHeight ?? 400)))
    }
  }

  const finishDrag = (event: ReactPointerEvent<HTMLDivElement>, cancelled: boolean) => {
    const drag = dragRef.current
    if (!drag || drag.id !== event.pointerId) return
    dragRef.current = null
    const elapsed = Math.max(1, event.timeStamp - drag.startT)
    const fling = drag.dy / elapsed > DISMISS_VELOCITY && drag.dy > 24
    if (!cancelled && (drag.dy > DISMISS_DISTANCE || fling)) {
      close(drag.dy)
      return
    }
    // Не дотянули — возвращаем на место.
    const sheet = sheetRef.current
    const overlay = overlayRef.current
    if (sheet) {
      sheet.style.transform = ""
      if (!reducedMotion()) {
        sheet.animate([{ transform: `translateY(${drag.dy}px)` }, { transform: "translateY(0)" }], { duration: 160, easing: EASE_OUT })
      }
    }
    if (overlay) overlay.style.opacity = ""
  }

  return (
    <DialogPrimitive.Root
      onOpenChange={(open) => {
        if (!open) close()
      }}
      open
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay asChild>
          <div className="fixed inset-0 z-50 bg-overlay" ref={overlayRef} />
        </DialogPrimitive.Overlay>
        <DialogPrimitive.Content
          aria-describedby={undefined}
          asChild
          onOpenAutoFocus={(event) => event.preventDefault()}
        >
          <div
            className={`fixed inset-x-0 bottom-0 z-50 mx-auto flex w-full max-w-xl flex-col overflow-hidden rounded-t-[var(--r-xl)]${tall ? " h-[100svh] max-h-[100svh]" : " max-h-[88svh]"} border-x-(length:--outline-w) border-t-(length:--outline-w) border-outline bg-popover text-text outline-none`}
            ref={sheetRef}
          >
            <DialogPrimitive.Title className="sr-only">{title}</DialogPrimitive.Title>
            <div
              className="shrink-0 cursor-grab touch-none px-4 pt-2 select-none active:cursor-grabbing"
              onPointerCancel={(event) => finishDrag(event, true)}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={(event) => finishDrag(event, false)}
            >
              <div aria-hidden="true" className="mx-auto mb-2 h-1.5 w-10 rounded-full bg-line-strong" />
              {header(() => close())}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-3">{children}</div>
            {footer ? (
              <div className="shrink-0 border-t-(length:--outline-w) border-line bg-popover px-4 pt-3 pb-[calc(0.75rem+var(--safe-bottom,0px))]">
                {footer}
              </div>
            ) : null}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/** Крестик шапки шторки. */
export function SheetCloseButton({ onClick }: { onClick: () => void }) {
  return (
    <Button aria-label="Закрыть" className="-mr-2 shrink-0" onClick={onClick} size="icon-sm" variant="ghost">
      <X />
    </Button>
  )
}
