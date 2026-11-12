import { DAY_MS, MSK, MSK_OFFSET_SEC, mskDate } from '#shared/msk'

const MIB = 1024 * 1024
const GIB = 1024 ** 3
export const WD_MON = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
const MONTHS_GEN = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

/** @internal */
export const pad2 = (x: number) => String(x).padStart(2, '0')

export const ru = (v: number, digits = 0) =>
  v.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits }).replace(/[\u00A0\u202F]/g, ' ')

/** Объём, база 1024: «1,4 ГБ», «380 МБ», меньше мегабайта - «12 КБ», ноль - «—». */
export function sizeBinRu(bytes: number | null | undefined): string {
  const n = bytes ?? 0
  if (n <= 0) return '—'
  const m = n / MIB
  if (m >= 1024) return `${(m / 1024).toFixed(1).replace('.', ',')} ГБ`
  if (m >= 1) return `${Math.round(m)} МБ`
  return `${Math.max(1, Math.round(n / 1024))} КБ`
}

/** Десятичные ГБ без единицы: знак после запятой до 10 ГБ. */
export const gbDecNumRu = (b: number) => ru(b / 1e9, b < 1e10 ? 1 : 0)
export const gbDecRu = (b: number | null | undefined) => b == null ? '—' : `${gbDecNumRu(b)} ГБ`
/** Диск: гибибайты целым, латиницей «GB»; объём - по свободному и его доле. */
export const gibLatin = (bytes: number) => `${Math.round(bytes / GIB)} GB`
export const gibTotalLatin = (free: number, pct: number | null | undefined) => pct ? `${Math.round(free / GIB / pct * 100)} GB` : '—'

/** Цифры из ввода → ДД.ММ.ГГГГ; точки только между группами, иначе backspace «залипает». */
export function maskRuDate(raw: string): string {
  const d = raw.replace(/\D/g, '').slice(0, 8)
  return [d.slice(0, 2), d.slice(2, 4), d.slice(4)].filter(Boolean).join('.')
}

/** «31.12.2026» → ISO полночи UTC; null - формат не распознан или даты нет (31.02). */
export function parseRuDateUtc(s: string): string | null {
  const m = s.trim().match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/)
  if (!m) return null
  const [dd, mm, yyyy] = [Number(m[1]), Number(m[2]), Number(m[3])]
  const d = new Date(Date.UTC(yyyy, mm - 1, dd))
  return d.getUTCDate() === dd && d.getUTCMonth() === mm - 1 ? d.toISOString() : null
}

/** ISO → «31.12.2026» по UTC, пусто без даты. */
export function formatRuDateUtc(iso: string | null | undefined): string {
  const d = new Date(iso ?? '')
  return iso && !Number.isNaN(d.getTime()) ? `${pad2(d.getUTCDate())}.${pad2(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}` : ''
}

export const dateRuLocal = (iso: string) => new Date(iso).toLocaleDateString('ru-RU')
export const expiryLabelLocal = (iso: string | null | undefined) => iso ? `до ${dateRuLocal(iso)}` : 'Бессрочный'

/** МСК: сегодня - «ЧЧ:ММ», раньше - «ДД.ММ». */
export function timeOrDdmmMsk(ms: number, now: number): string {
  const d = new Date(ms)
  if (mskDate(ms) === mskDate(now)) return d.toLocaleTimeString('ru-RU', { timeZone: MSK, hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString('ru-RU', { timeZone: MSK, day: '2-digit', month: '2-digit' })
}

// Подписи по Москве: сдвиг на +3 ч и чтение как UTC.
const mskShifted = (sec: number) => new Date((sec + MSK_OFFSET_SEC) * 1000)
export const mskHourFromSec = (sec: number) => mskShifted(sec).getUTCHours()
export const hourRangeRu = (h: number) => `${pad2(h)}:00–${pad2((h + 1) % 24)}:00`
const weekdayDate = (t: Date) => `${WD_MON[(t.getUTCDay() + 6) % 7]}, ${t.getUTCDate()} ${MONTHS_GEN[t.getUTCMonth()]}`
/** «вт, 10 ноя» МСК по unix-секундам и по номеру суток МСК. */
export const mskWeekdayDateFromSec = (sec: number) => weekdayDate(mskShifted(sec))
export const mskDayNumLabel = (day: number) => weekdayDate(new Date(day * DAY_MS))

/** Зона браузера: «сегодня ЧЧ:ММ» / «вчера ЧЧ:ММ» / «ДД.ММ ЧЧ:ММ». */
export function todayYesterdayHmLocal(iso: string, day = new Date()): string {
  const d = new Date(iso)
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  if (d.toDateString() === day.toDateString()) return `сегодня ${hm}`
  day.setDate(day.getDate() - 1)
  return d.toDateString() === day.toDateString() ? `вчера ${hm}` : `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)} ${hm}`
}

/** Зона браузера: сегодня (today - toDateString) «ЧЧ:ММ:СС», иначе «ДД.ММ ЧЧ:ММ». */
export function hmsOrDdmmHmLocal(t: string, today: string): string {
  const d = new Date(t)
  const hms = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  return d.toDateString() === today ? hms : `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)} ${hms.slice(0, 5)}`
}

export const initials = (name: string) => name.split(' ').filter(Boolean).map(s => s[0]).join('').slice(0, 2)

/** Сколько суток до конца срока (вверх), если активный клиент истекает в ближайшую неделю. */
export function expiringInDays(expiresAt: string | null, status: string, now = Date.now()): number | null {
  const left = expiresAt && status === 'active' ? new Date(expiresAt).getTime() - now : 0
  return left > 0 && left <= 7 * DAY_MS ? Math.ceil(left / DAY_MS) : null
}
