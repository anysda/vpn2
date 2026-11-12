export interface NodeMetric {
  tag: string
  label: string
  host: string | null
  instance: string
  cpu: number | null
  iowait: number | null
  steal: number | null
  ram: number | null
  rxMbps: number | null
  txMbps: number | null
  uptimeSec: number | null
  diskFreeBytes: number | null
  diskFreePct: number | null
  /** Возраст последних метрик, сек; null - данных нет. */
  staleSec: number | null
  /** Байты через WAN с 00:00 МСК. */
  rxTodayBytes: number | null
  txTodayBytes: number | null
  /** Экзит выключен из панели: watchdog обходит его как мёртвый. */
  disabled: boolean
  /** Через экзит сейчас идёт иностранный трафик вне дорожек. */
  active: boolean
  activeDevices: number | null
  lanes: number | null
  lanesWarp: number | null
}

const regions = new Intl.DisplayNames(['ru'], { type: 'region' })
/** nl → «Нидерланды», entry - «Entry». */
export function exitName(tag: string): string {
  if (tag === 'ru') return 'Entry'
  try {
    const name = regions.of(tag.toUpperCase())
    return name && name !== tag.toUpperCase() ? name : tag.toUpperCase()
  }
  catch {
    return tag.toUpperCase()
  }
}

export function uptimeShort(sec: number | null | undefined): string {
  if (!sec || sec < 0) return '—'
  if (sec >= 86400) return `${Math.floor(sec / 86400)} д`
  return sec >= 3600 ? `${Math.floor(sec / 3600)} ч` : `${Math.max(1, Math.floor(sec / 60))} мин`
}

export const pct = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? '—' : `${Math.round(v)}%`
/** Порог для подсветки значения: w - жёлтый, c - красный. */
export const level = (v: number | null | undefined, warn: number, crit: number) => v == null ? '' : v >= crit ? 'c' : v >= warn ? 'w' : ''

/** Состояние карточки узла: метрик нет 5 с - офлайн; iowait/steal от 30%, CPU/RAM от 80% - медленно. */
export function nodeHealth(n: NodeMetric) {
  if (n.staleSec == null || n.staleSec >= 5) {
    const ago = n.staleSec == null ? 'нет метрик' : `метрик нет ${n.staleSec >= 60 ? `${Math.floor(n.staleSec / 60)} мин` : `${Math.floor(n.staleSec)} с`}`
    return { kind: 'offline', tag: 'Офлайн', big: '—', bigLabel: 'CPU', meta: ago }
  }
  const io = (n.iowait ?? 0) >= 30, st = (n.steal ?? 0) >= 30
  const meta = `CPU ${pct(n.cpu)} · RAM ${pct(n.ram)}`
  if (io) return { kind: 'slow', tag: 'Медленно', big: pct(n.iowait), bigLabel: 'iowait', meta }
  if (st) return { kind: 'slow', tag: 'Медленно', big: pct(n.steal), bigLabel: 'steal', meta }
  const slow = (n.cpu ?? 0) >= 80 || (n.ram ?? 0) >= 80
  return { kind: slow ? 'slow' : 'ok', tag: slow ? 'Медленно' : 'В норме', big: pct(n.cpu), bigLabel: 'CPU', meta: `iowait ${pct(n.iowait)} · RAM ${pct(n.ram)}` }
}

/** Entry первым, дальше экзиты по тегу. */
export const sortNodes = (list: NodeMetric[]) => [...list].sort((a, b) => a.tag === 'ru' ? -1 : b.tag === 'ru' ? 1 : a.tag.localeCompare(b.tag))

/** Окно из `len` соседних точек с наибольшей суммой - подсвеченная полоса графика. */
export function peakBand(data: number[], len = 4): [number, number] | null {
  if (data.length < len) return null
  let best = 0, sum = -Infinity
  for (let i = 0; i + len <= data.length; i++) {
    const s = data.slice(i, i + len).reduce((a, b) => a + b, 0)
    if (s >= sum) [best, sum] = [i, s]
  }
  return [best, best + len - 1]
}
