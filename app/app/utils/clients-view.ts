import { pluralRu } from '#shared/plural'

const MIB = 1024 * 1024
const DAY_MS = 86_400_000
/** «Истекают»: активный клиент, у которого срок кончится в ближайшие 7 суток. */
const EXPIRING_MS = 7 * DAY_MS

/** Объём как в макете: «1,4 ГБ», «380 МБ», меньше мегабайта — «12 КБ», ноль — «—». */
export function sizeRu(bytes: number | null | undefined): string {
  const n = bytes ?? 0
  if (n <= 0) return '—'
  const m = n / MIB
  if (m >= 1024) return `${(m / 1024).toFixed(1).replace('.', ',')} ГБ`
  if (m >= 1) return `${Math.round(m)} МБ`
  return `${Math.max(1, Math.round(n / 1024))} КБ`
}

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
  return `через ${n} ${pluralRu(n, ['день', 'дня', 'дней'])}`
}

const EASE = 'cubic-bezier(.23,1,.32,1)'

/** Анимация из макета: WAAPI, «уменьшить движение» её выключает. */
export function anim(el: Element | null | undefined, keyframes: Keyframe[], o: KeyframeAnimationOptions) {
  if (!el || matchMedia('(prefers-reduced-motion: reduce)').matches) return null
  return el.animate(keyframes, { easing: EASE, fill: 'backwards', ...o })
}
