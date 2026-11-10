import { ru } from './overview'

// Формат объёма, дат и срока панели. Разновидности намеренно разные (база 1024
// против 1e9, UTC против МСК против зоны браузера) — имя говорит, какая где.

const MIB = 1024 * 1024
const GIB = 1024 ** 3
const MSK = 'Europe/Moscow'
const MSK_OFFSET_SEC = 3 * 3600

/** Неделя с понедельника и с воскресенья (под getUTCDay), месяцы в родительном. */
export const WD_MON = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
const WD_SUN = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб']
const MONTHS_GEN = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

export const pad2 = (x: number) => String(x).padStart(2, '0')

/** Объём как в макете, база 1024: «1,4 ГБ», «380 МБ», меньше мегабайта — «12 КБ», ноль — «—». */
export function sizeBinRu(bytes: number | null | undefined): string {
  const n = bytes ?? 0
  if (n <= 0) return '—'
  const m = n / MIB
  if (m >= 1024) return `${(m / 1024).toFixed(1).replace('.', ',')} ГБ`
  if (m >= 1) return `${Math.round(m)} МБ`
  return `${Math.max(1, Math.round(n / 1024))} КБ`
}

/** Десятичные гигабайты (1e9) числом. */
export const bytesToGbDec = (b: number) => b / 1e9
/** Десятичные ГБ без единицы: знак после запятой до 10 ГБ. */
export const gbDecNumRu = (b: number) => ru(b / 1e9, b < 1e10 ? 1 : 0)
/** Десятичные ГБ с единицей «ГБ», нет данных — «—». */
export const gbDecRu = (b: number | null | undefined) => b == null ? '—' : `${gbDecNumRu(b)} ГБ`
/** Десятичные ГБ, округлённые до 0,1, числом. */
export const gbDecRound1 = (b: number) => Math.round(b / 1e8) / 10

/** Диск: гибибайты (1024³) целым, латиницей «GB». */
export const gibLatin = (bytes: number) => `${Math.round(bytes / GIB)} GB`
/** Объём диска по свободному месту и его доле в процентах, латиницей «GB». */
export function gibTotalLatin(freeBytes: number, freePct: number | null | undefined): string {
  const free = freeBytes / GIB
  return freePct ? `${Math.round(free / freePct * 100)} GB` : '—'
}

/** «31.12.2026» → ISO-строка (UTC-полночь). null — если формат не распознан. */
export function parseRuDateUtc(s: string): string | null {
  const m = s.trim().match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/)
  if (!m) return null
  const dd = Number(m[1])
  const mm = Number(m[2])
  const yyyy = Number(m[3])
  const d = new Date(Date.UTC(yyyy, mm - 1, dd))
  // Отсеять несуществующие даты (напр. 31.02.2026).
  if (d.getUTCDate() !== dd || d.getUTCMonth() !== mm - 1) return null
  return d.toISOString()
}

/** ISO-строка → «31.12.2026» по UTC (пусто, если даты нет). */
export function formatRuDateUtc(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${pad2(d.getUTCDate())}.${pad2(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`
}

/** Дата «ДД.ММ.ГГГГ» в зоне браузера. */
export const dateRuLocal = (iso: string) => new Date(iso).toLocaleDateString('ru-RU')

/** Срок: «Бессрочный» или «до ДД.ММ.ГГГГ» в зоне браузера. */
export function expiryLabelLocal(expiresAt: string | null | undefined): string {
  if (!expiresAt) return 'Бессрочный'
  return `до ${dateRuLocal(expiresAt)}`
}

const mskDayOf = (ms: number) => new Date(ms).toLocaleDateString('ru-RU', { timeZone: MSK })
/** МСК: сегодня — «ЧЧ:ММ», раньше — «ДД.ММ». */
export function timeOrDdmmMsk(ms: number, now: number): string {
  if (mskDayOf(ms) === mskDayOf(now)) {
    return new Date(ms).toLocaleTimeString('ru-RU', { timeZone: MSK, hour: '2-digit', minute: '2-digit' })
  }
  return new Date(ms).toLocaleDateString('ru-RU', { timeZone: MSK, day: '2-digit', month: '2-digit' })
}

// Подписи по Москве: сдвигаем на +3 ч и читаем как UTC.
const mskShifted = (sec: number) => new Date((sec + MSK_OFFSET_SEC) * 1000)
/** Час МСК по unix-секундам. */
export const mskHourFromSec = (sec: number) => mskShifted(sec).getUTCHours()
/** «ЧЧ:00–ЧЧ:00» на час вперёд. */
export const hourRangeRu = (h: number) => `${pad2(h)}:00–${pad2((h + 1) % 24)}:00`
/** «вт, 10 ноя» МСК по unix-секундам. */
export function mskWeekdayDateFromSec(sec: number): string {
  const t = mskShifted(sec)
  return `${WD_SUN[t.getUTCDay()]}, ${t.getUTCDate()} ${MONTHS_GEN[t.getUTCMonth()]}`
}
/** «пн, 10 ноя» по номеру суток МСК. */
export function mskDayNumLabel(day: number): string {
  const t = new Date(day * 864e5)
  return `${WD_MON[(t.getUTCDay() + 6) % 7]}, ${t.getUTCDate()} ${MONTHS_GEN[t.getUTCMonth()]}`
}

/** Зона браузера: «сегодня ЧЧ:ММ» / «вчера ЧЧ:ММ» / «ДД.ММ ЧЧ:ММ». */
export function todayYesterdayHmLocal(iso: string, day = new Date()): string {
  const d = new Date(iso)
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  if (d.toDateString() === day.toDateString()) return `сегодня ${hm}`
  day.setDate(day.getDate() - 1)
  if (d.toDateString() === day.toDateString()) return `вчера ${hm}`
  return `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)} ${hm}`
}

/** Зона браузера: сегодня (today — toDateString) «ЧЧ:ММ:СС», иначе «ДД.ММ ЧЧ:ММ». */
export function hmsOrDdmmHmLocal(t: string, today: string): string {
  const d = new Date(t)
  const hms = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  return d.toDateString() === today ? hms : `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)} ${hms.slice(0, 5)}`
}

/** «ДД.ММ» в зоне браузера. */
export const ddmmLocal = (d: Date) => `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}`
