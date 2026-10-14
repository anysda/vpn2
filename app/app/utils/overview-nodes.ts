import { nodeState, type NodeMetric } from '~/composables/useMonitoring'

// Узел на обзоре: имя, подпись и состояние карточки. Пороги те же, что были
// у плиток мониторинга: CPU и RAM 60/80, iowait и steal 10/30.

const regions = (() => {
  try {
    return new Intl.DisplayNames(['ru'], { type: 'region' })
  }
  catch {
    return null
  }
})()

/** Имя экзита по тегу страны: nl → «Нидерланды». Entry — «Entry». */
export function exitName(tag: string): string {
  if (tag === 'ru') return 'Entry'
  const up = tag.toUpperCase()
  try {
    const name = regions?.of(up)
    return name && name !== up ? name : up
  }
  catch {
    return up
  }
}

/** Аптайм коротко, как в макете: «41 д», «5 ч», «12 мин». */
export function uptimeShort(sec: number | null | undefined): string {
  if (!sec || sec < 0) return '—'
  if (sec >= 86400) return `${Math.floor(sec / 86400)} д`
  if (sec >= 3600) return `${Math.floor(sec / 3600)} ч`
  return `${Math.max(1, Math.floor(sec / 60))} мин`
}

/** Подпись под именем: entry — адрес и аптайм, экзит — тег, пинг, WARP. */
export function nodeSubtitle(n: NodeMetric, pingMs: number | null | undefined): string {
  if (n.tag === 'ru') return [n.host, uptimeShort(n.uptimeSec)].filter(Boolean).join(' · ')
  const parts = [n.tag.toUpperCase()]
  if (pingMs != null) parts.push(`${pingMs} мс`)
  if (n.lanesWarp) parts.push('WARP')
  return parts.join(' · ')
}

const pct = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? '—' : `${Math.round(v)}%`

/** Класс порога для модалки: w — жёлтый, c — красный. */
export function level(v: number | null | undefined, warn: number, crit: number): '' | 'w' | 'c' {
  if (v == null) return ''
  return v >= crit ? 'c' : v >= warn ? 'w' : ''
}

export interface NodeHealth {
  kind: 'ok' | 'slow' | 'offline'
  // карточка подсвечена (медленно или пропал)
  lit: boolean
  tag: string
  big: string
  bigLabel: string
  meta: string
}

export function nodeHealth(n: NodeMetric): NodeHealth {
  if (nodeState(n.staleSec) !== 'online') {
    const ago = n.staleSec == null ? 'нет метрик' : `метрик нет ${n.staleSec >= 60 ? `${Math.floor(n.staleSec / 60)} мин` : `${Math.floor(n.staleSec)} с`}`
    return { kind: 'offline', lit: true, tag: 'Офлайн', big: '—', bigLabel: 'CPU', meta: ago }
  }
  const io = (n.iowait ?? 0) >= 30, st = (n.steal ?? 0) >= 30
  const slow = io || st || (n.cpu ?? 0) >= 80 || (n.ram ?? 0) >= 80
  if (io) return { kind: 'slow', lit: true, tag: 'Медленно', big: pct(n.iowait), bigLabel: 'iowait', meta: `CPU ${pct(n.cpu)} · RAM ${pct(n.ram)}` }
  if (st) return { kind: 'slow', lit: true, tag: 'Медленно', big: pct(n.steal), bigLabel: 'steal', meta: `CPU ${pct(n.cpu)} · RAM ${pct(n.ram)}` }
  return {
    kind: slow ? 'slow' : 'ok',
    lit: slow,
    tag: slow ? 'Медленно' : 'В норме',
    big: pct(n.cpu),
    bigLabel: 'CPU',
    meta: `iowait ${pct(n.iowait)} · RAM ${pct(n.ram)}`,
  }
}

/** Entry первым, дальше экзиты по тегу. */
export function sortNodes(list: NodeMetric[]): NodeMetric[] {
  return [...list].sort((a, b) => a.tag === 'ru' ? -1 : b.tag === 'ru' ? 1 : a.tag.localeCompare(b.tag))
}
