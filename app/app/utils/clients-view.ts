import { plural } from '#shared/plural'

const DAY_MS = 86_400_000
/** «Истекают»: активный клиент, у которого срок кончится в ближайшие 7 суток. */
const EXPIRING_MS = 7 * DAY_MS

/** Инициалы для аватара: первые буквы первых двух слов. */
export function initials(name: string): string {
  return name.split(' ').filter(Boolean).map(s => s[0]).join('').slice(0, 2)
}

/** Сколько суток до конца срока (вверх), если клиент истекает в ближайшую неделю. */
export function expiringInDays(expiresAt: string | null, status: string, now = Date.now()): number | null {
  if (!expiresAt || status !== 'active') return null
  const left = new Date(expiresAt).getTime() - now
  if (!(left > 0) || left > EXPIRING_MS) return null
  return Math.ceil(left / DAY_MS)
}

export function inDaysRu(n: number): string {
  return `через ${n} ${plural(n, 'day')}`
}

const EASE = 'cubic-bezier(.23,1,.32,1)'

/** Анимация из макета: WAAPI, «уменьшить движение» её выключает. */
export function anim(el: Element | null | undefined, keyframes: Keyframe[], o: KeyframeAnimationOptions) {
  if (!el || matchMedia('(prefers-reduced-motion: reduce)').matches) return null
  return el.animate(keyframes, { easing: EASE, fill: 'backwards', ...o })
}

/** Текст ошибки сервера или запасной заголовок тоста. */
export function errTitle(e: unknown, fallback: string): string {
  return (e as { statusMessage?: string }).statusMessage ?? fallback
}
