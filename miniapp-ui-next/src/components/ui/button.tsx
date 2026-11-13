import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

// «Чанк»-кнопка лендинга: плотный контур + смещённая тень, нажимается вниз
// (.btn в frontend/css/base.css). Варианты читают токены, поэтому темы
// переключаются без правок здесь. Размеры держат тач-таргет не меньше 44px.
const chunk =
  "chunk-press border-(length:--outline-w) border-outline shadow-[var(--chunk-shadow-sm)] disabled:shadow-none"

const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center gap-2 rounded-full whitespace-nowrap outline-none select-none focus-visible:ring-3 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        // Главное действие экрана — фиолет, как .btn-violet на лендинге.
        default: cn(chunk, "bg-brand text-brand-ink hover:bg-brand-hover"),
        secondary: cn(chunk, "bg-surface-1 text-text hover:bg-surface-2"),
        outline: cn(chunk, "bg-transparent text-text hover:bg-surface-2"),
        ghost:
          "border-(length:--outline-w) border-transparent text-text-muted transition-colors hover:bg-surface-2 hover:text-text active:bg-surface-3",
        destructive: cn(chunk, "bg-surface-1 text-danger hover:bg-surface-2"),
        link: "text-text underline underline-offset-4",
      },
      size: {
        sm: "h-10 px-4 text-label [&_svg:not([class*='size-'])]:size-3.5",
        default: "h-11 px-5 text-label [&_svg:not([class*='size-'])]:size-4",
        lg: "h-14 px-6 text-body font-bold [&_svg:not([class*='size-'])]:size-4",
        icon: "size-11 [&_svg:not([class*='size-'])]:size-5",
        "icon-sm": "size-9 [&_svg:not([class*='size-'])]:size-4",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
