// Разбор ответов AdGuard Home для вкладки DNS. Чистые функции без окружения
// Nitro: их гоняют тесты через serverModule.

interface AghRule { text: string, filter_list_id: number }

/** Ответ /control/filtering/check_host. */
export interface CheckHost {
  reason: string
  rules?: AghRule[] | null
  service_name?: string
  cname?: string
  ip_addrs?: string[] | null
}

export interface AghLogEntry {
  answer?: Array<{ type: string, value: string }> | null
  cached?: boolean
  client: string
  client_info?: { name?: string } | null
  elapsedMs?: string
  question: { name: string, type: string }
  reason: string
  rules?: AghRule[] | null
  service_name?: string
  time: string
  upstream?: string
}

type Top = Array<Record<string, number>>

export interface AghStats {
  time_units: 'hours' | 'days'
  num_dns_queries: number
  num_blocked_filtering: number
  num_replaced_safebrowsing: number
  num_replaced_parental: number
  avg_processing_time: number
  dns_queries: number[]
  blocked_filtering: number[]
  replaced_safebrowsing: number[]
  replaced_parental: number[]
  top_queried_domains: Top
  top_clients: Top
  top_blocked_domains: Top
  top_upstreams_responses: Top
  top_upstreams_avg_time: Top
}

type LogKind = 'ok' | 'blk' | 'rw' | 'safe'
export type LogFilter = 'all' | 'blk' | 'ok' | 'rw'

export interface LogRow {
  time: string
  domain: string
  type: string
  kind: LogKind
  detail: string
  client: string
  ip: string
}

const BLOCKED = new Set(['FilteredBlackList', 'FilteredBlockedService', 'FilteredInvalid'])
const REWRITTEN = new Set(['Rewrite', 'RewriteEtcHosts', 'RewriteRule', 'FilteredSafeSearch'])

/** Название источника правила: 0 — свои правила, остальное — список по id. */
function listName(id: number | undefined, lists: Map<number, string>): string {
  if (!id) return 'Свои правила'
  return lists.get(id) ?? `Список ${id}`
}

/** Сервер выше без схемы и порта: «tls://dns.google:853» → «dns.google». */
function upstreamHost(u: string): string {
  const m = u.match(/^(?:[a-z0-9]+:\/\/)?(\[[^\]]+\]|[^/:]+)/i)
  return m?.[1] ?? u
}

/** Миллисекунды по-русски: меньше 10 — с одним знаком, дальше целыми. */
function fmtMs(ms: number): string {
  const v = ms < 10 ? Math.round(ms * 10) / 10 : Math.round(ms)
  return String(v).replace('.', ',')
}

/** Состояние строки журнала и подпись под ним. */
function classify(e: AghLogEntry, lists: Map<number, string>, services: Map<string, string>): { kind: LogKind, detail: string } {
  if (e.reason === 'FilteredSafeBrowsing') return { kind: 'safe', detail: 'Безопасная навигация' }
  if (e.reason === 'FilteredParental') return { kind: 'safe', detail: 'Родительский контроль' }
  if (BLOCKED.has(e.reason)) {
    if (e.reason === 'FilteredBlockedService') {
      const id = e.service_name ?? ''
      return { kind: 'blk', detail: `Сервис: ${services.get(id) ?? id}` }
    }
    return { kind: 'blk', detail: listName(e.rules?.[0]?.filter_list_id, lists) }
  }
  if (REWRITTEN.has(e.reason)) {
    const a = (e.answer ?? []).find(x => x.value)?.value
    return { kind: 'rw', detail: `→ ${a ?? 'пусто'}` }
  }
  if (e.reason === 'NotFilteredWhiteList') return { kind: 'ok', detail: listName(e.rules?.[0]?.filter_list_id, lists) }
  if (e.cached) return { kind: 'ok', detail: 'из кэша' }
  const ms = Number(e.elapsedMs ?? 0)
  return { kind: 'ok', detail: e.upstream ? `${upstreamHost(e.upstream)} · ${fmtMs(ms)} мс` : `${fmtMs(ms)} мс` }
}

/** Строка журнала с клиентом по имени из панели. */
export function logRow(e: AghLogEntry, names: Map<string, string>, lists: Map<number, string>, services: Map<string, string>): LogRow {
  const { kind, detail } = classify(e, lists, services)
  return {
    time: e.time,
    domain: e.question.name,
    type: e.question.type,
    kind,
    detail,
    client: names.get(e.client) ?? e.client_info?.name ?? e.client,
    ip: e.client,
  }
}

/** Подходит ли строка под фильтр журнала и строку поиска. */
export function logMatch(r: LogRow, f: LogFilter, q: string): boolean {
  if (f === 'blk' ? r.kind !== 'blk' && r.kind !== 'safe' : f !== 'all' && r.kind !== f) return false
  if (!q) return true
  const s = q.toLowerCase()
  return r.domain.toLowerCase().includes(s) || r.client.toLowerCase().includes(s) || r.ip.includes(s)
}

/**
 * Свои правила при блокировке или разблокировке домена из журнала: убираем
 * точное противоположное правило, новое — отдельно, если без него не вышло.
 */
export function toggleRules(rules: string[], domain: string, block: boolean): { rules: string[], removed: boolean, rule: string } {
  const allow = `@@||${domain}^`
  const deny = `||${domain}^`
  const opposite = block ? allow : deny
  const kept = rules.filter(r => r.trim() !== opposite)
  return { rules: kept, removed: kept.length !== rules.length, rule: block ? deny : allow }
}

const pair = (o: Record<string, number>): [string, number] => Object.entries(o)[0] ?? ['', 0]

export interface Summary {
  units: 'hours' | 'days'
  queries: number
  blocked: number
  blockedPct: number
  threats: number
  avgMs: number
  cachedPct: number | null
  q: number[]
  b: number[]
  t: number[]
  ms: number[]
  upstreams: Array<{ url: string, pct: number, ms: number }>
  mode: string
  clients: Array<[string, number]>
  domains: Array<[string, number]>
  blocks: Array<[string, number]>
}

const MODES: Record<string, string> = { '': 'балансировка', 'load_balance': 'балансировка', 'parallel': 'параллельно', 'fastest_addr': 'самый быстрый' }

/**
 * Сводка: при почасовой статистике — последние сутки по рядам AdGuard (при
 * интервале 24 ч это ровно num_*), при подневной — весь интервал. Доля
 * ответов из кэша и ход времени ответа — по последним записям журнала.
 */
export function summarize(s: AghStats, names: Map<string, string>, mode: string, recent: AghLogEntry[]): Summary {
  const n = s.time_units === 'hours' ? 24 : s.dns_queries.length
  const last = (a: number[] | undefined) => (a ?? []).slice(-n)
  const sum = (a: number[]) => a.reduce((x, y) => x + y, 0)
  const q = last(s.dns_queries)
  const b = last(s.blocked_filtering)
  const sb = last(s.replaced_safebrowsing)
  const t = last(s.replaced_parental).map((v, i) => v + (sb[i] ?? 0))
  const queries = sum(q)
  const blocked = sum(b)

  const answered = recent.filter(e => e.reason.startsWith('NotFiltered'))
  const cachedPct = answered.length ? Math.round(answered.filter(e => e.cached).length / answered.length * 100) : null
  const chrono = recent.filter(e => !e.cached && e.upstream).reverse()
  const step = Math.max(1, Math.ceil(chrono.length / 12))
  const ms: number[] = []
  for (let i = 0; i < chrono.length; i += step) {
    const part = chrono.slice(i, i + step)
    ms.push(sum(part.map(e => Number(e.elapsedMs ?? 0))) / part.length)
  }

  const total = sum(s.top_upstreams_responses.map(o => pair(o)[1]))
  const times = new Map(s.top_upstreams_avg_time.map(pair))
  const upstreams = s.top_upstreams_responses.slice(0, 4).map((o) => {
    const [url, c] = pair(o)
    return { url, pct: total ? Math.round(c / total * 100) : 0, ms: (times.get(url) ?? 0) * 1000 }
  })

  const byName = new Map<string, number>()
  for (const o of s.top_clients) {
    const [ip, c] = pair(o)
    const k = names.get(ip) ?? ip
    byName.set(k, (byName.get(k) ?? 0) + c)
  }
  const top = (l: Top) => l.map(pair).slice(0, 7)

  return {
    units: s.time_units,
    queries,
    blocked,
    blockedPct: queries ? blocked / queries * 100 : 0,
    threats: sum(t),
    avgMs: s.avg_processing_time * 1000,
    cachedPct,
    q,
    b,
    t,
    ms,
    upstreams,
    mode: MODES[mode] ?? mode,
    clients: [...byName].sort((x, y) => y[1] - x[1]).slice(0, 7),
    domains: top(s.top_queried_domains),
    blocks: top(s.top_blocked_domains),
  }
}
