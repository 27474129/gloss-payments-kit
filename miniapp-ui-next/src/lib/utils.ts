import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

/**
 * tailwind-merge не знает про наши кастомные v4-токены. Оно классифицирует
 * произвольный класс `text-*` эвристикой: если это не список ключевых слов
 * шрифта (text-xs…text-9xl) и не квадратные скобки — считает цветом текста.
 * Наши `text-title`, `text-body` и т.п. — это РАЗМЕР шрифта, но по этому
 * правилу twMerge относил их к той же группе, что и `text-text`/`text-brand-ink`
 * (цвет), и «побеждал» тот класс, что стоит в строке классов последним —
 * то есть цвет молча вырезался, а текст наследовал белый от <body>. Отсюда
 * нечитаемый белый текст на кнопках. Явно разносим группы, чтобы конфликта
 * больше не было в принципе — не только на этой кнопке.
 */
export const cn = (() => {
  const twMerge = extendTailwindMerge({
    extend: {
      classGroups: {
        "font-size": [
          "text-display",
          "text-title",
          "text-headline",
          "text-body",
          "text-label",
          "text-caption",
        ],
        "text-color": [
          "text-text",
          "text-text-muted",
          "text-text-faint",
          "text-brand-ink",
        ],
      },
    },
  })
  return (...inputs: ClassValue[]) => twMerge(clsx(inputs))
})()
