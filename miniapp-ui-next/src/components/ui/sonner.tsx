import { toast, Toaster as Sonner } from "sonner"

import { Button } from "@/components/ui/button"

type NoticeVariant = "success" | "error" | "info" | "warning"

type NoticeAction = {
  label: string
  onClick: () => void
}

type MiniAppToastOptions = {
  variant: NoticeVariant
  title: string
  description: string
  action?: NoticeAction
  /** Тихая вторая кнопка под главной (например, «Заработать бесплатно»). */
  secondaryAction?: NoticeAction
  duration?: number
}

// Индикатор тона тоста — единственное место, где цвет несёт смысл
// "успех/тревога/ошибка", а не бренд и не действие: --brand означает
// "нажми сюда", а тост ничего не ждёт от тапа. Успех — оранжевый --accent-3
// (тёплая «хорошая новость», как бейдж на лендинге).
const TONE_CLASS: Record<NoticeVariant, string> = {
  success: "bg-accent-3",
  warning: "bg-warning",
  error: "bg-destructive",
  info: "bg-text-muted",
}

function MiniAppToast({
  id,
  variant,
  title,
  description,
  action,
  secondaryAction,
}: Omit<MiniAppToastOptions, "duration"> & { id: string | number }) {
  return (
    <section className="chunk relative mx-3 flex w-[calc(100vw-1.5rem)] max-w-md flex-col rounded-[var(--r-xl)] bg-popover px-5 pt-8 pb-6 text-text">
      {/* Ручка-хват — единственный намёк на смахивание; текстовой подсказки
          снизу больше нет (низкий контраст, дублировала и ручку, и
          авто-скрытие). */}
      <span aria-hidden="true" className="absolute top-3 left-1/2 h-1.5 w-10 -translate-x-1/2 rounded-full bg-text/25" />
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" className={`size-3 shrink-0 rounded-full border-(length:--outline-w) border-outline ${TONE_CLASS[variant]}`} />
          <h2 className="text-headline text-text">{title}</h2>
        </div>
        <p className="mt-1.5 text-body text-text-muted">{description}</p>
      </div>
      {action ? (
        <Button
          className="mt-6 w-full"
          onClick={() => {
            action.onClick()
            toast.dismiss(id)
          }}
          size="lg"
        >
          {action.label}
        </Button>
      ) : null}
      {secondaryAction ? (
        <Button
          className="mt-2 w-full"
          onClick={() => {
            secondaryAction.onClick()
            toast.dismiss(id)
          }}
          variant="ghost"
        >
          {secondaryAction.label}
        </Button>
      ) : null}
    </section>
  )
}

function showMiniAppToast({ duration = 7_000, ...options }: MiniAppToastOptions) {
  return toast.custom((id) => <MiniAppToast id={id} {...options} />, { duration })
}

/** Единственный `<Toaster>` мини-аппа. `floating={true}`, когда над
 *  контентом висит нижняя навигация (см. --floating-pb-nav в index.css) —
 *  тост сдвигается над панелью вместо самого низа экрана. */
function MiniAppToaster({ floating = false }: { floating?: boolean }) {
  const bottom = floating ? "var(--floating-pb-nav)" : "0px"
  return (
    <Sonner
      className="toaster"
      closeButton={false}
      mobileOffset={{ bottom, left: "0px", right: "0px" }}
      offset={{ bottom, left: "0px", right: "0px" }}
      position="bottom-center"
      swipeDirections={["bottom"]}
      theme="light"
      visibleToasts={1}
    />
  )
}

export { showMiniAppToast, MiniAppToaster }
