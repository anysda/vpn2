/**
 * Лимит неудачных попыток входа и проверки кода 2FA (VPN2-162).
 *
 * Встроенный ограничитель better-auth считает каждый запрос, удачный тоже, и
 * отсчитывает окно от последнего пропущенного запроса: десять верных входов
 * подряд с паузами меньше окна дают блокировку. Здесь считаются только
 * неудачи, а окно фиксированное: от первой неудачи до window секунд после неё.
 *
 * Попытка занимает место в счёте до ответа: параллельная пачка запросов не
 * проскочит мимо лимита, пока первые ещё проверяются. Удачный ответ место
 * освобождает, неудачный оставляет как неудачу.
 */

interface Entry {
  failures: number
  pending: number
  /** Начало окна: время первой неудачи, 0 — неудач ещё не было. */
  since: number
}

const entries = new Map<string, Entry>()
const MAX_ENTRIES = 100_000

function prune(now: number, windowMs: number) {
  if (entries.size < MAX_ENTRIES) return
  for (const [key, e] of entries) {
    if (e.pending === 0 && now - e.since >= windowMs) entries.delete(key)
  }
}

/**
 * Занять попытку. null — можно идти дальше, затем обязательно позвать
 * finish(key, ok). Число — попытки исчерпаны, через столько секунд окно
 * закончится.
 */
export function loginAttempt(key: string, max: number, windowSec: number): number | null {
  const now = Date.now()
  const windowMs = windowSec * 1000
  prune(now, windowMs)
  let e = entries.get(key)
  if (e && e.since && now - e.since >= windowMs) {
    e.failures = 0
    e.since = 0
  }
  if (!e) {
    e = { failures: 0, pending: 0, since: 0 }
    entries.set(key, e)
  }
  if (e.failures + e.pending >= max) {
    return e.since ? Math.max(1, Math.ceil((e.since + windowMs - now) / 1000)) : windowSec
  }
  e.pending++
  return null
}

export function loginFinish(key: string, ok: boolean) {
  const e = entries.get(key)
  if (!e) return
  e.pending = Math.max(0, e.pending - 1)
  if (!ok) {
    e.failures++
    if (!e.since) e.since = Date.now()
  }
  if (e.pending === 0 && e.failures === 0) entries.delete(key)
}
