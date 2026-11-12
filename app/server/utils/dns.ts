import { isIP } from 'node:net'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'

type Top = Array<Record<string, number>>
interface AghRule { text: string, filter_list_id: number }
export interface CheckHost { reason: string, rules?: AghRule[] | null, service_name?: string, cname?: string, ip_addrs?: string[] | null }
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
export interface AghStats {
  time_units: 'hours' | 'days'
  num_dns_queries: number
  num_blocked_filtering: number
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
export interface AghFilter { id: number, name: string, url: string, enabled: boolean, rules_count: number, last_updated?: string }
export interface FilteringStatus { interval: number, filters: AghFilter[] | null, whitelist_filters: AghFilter[] | null, user_rules: string[] | null }
export interface Rewrite { domain: string, answer: string, enabled: boolean }
export interface AghPersistentClient {
  name: string
  ids: string[]
  use_global_settings: boolean
  filtering_enabled: boolean
  safesearch_enabled?: boolean
  safe_search?: { enabled: boolean }
  use_global_blocked_services: boolean
  blocked_services: string[] | null
}

function authHeader(): Record<string, string> {
  const cfg = useRuntimeConfig()
  const user = String(cfg.aghUser ?? '')
  const pass = String(cfg.aghPassword ?? '')
  return user && pass ? { Authorization: `Basic ${Buffer.from(`${user}:${pass}`).toString('base64')}` } : {}
}

interface AghCall { method?: 'GET' | 'POST' | 'PUT', body?: Record<string, unknown>, query?: Record<string, string | number | undefined>, timeout?: number }

/** Отказ AdGuard (4xx с текстом) уходит человеку как 400 с его текстом, нет связи или неверные креды - 502. */
export async function agh<T>(path: string, call: AghCall = {}): Promise<T> {
  try {
    const res: unknown = await $fetch(`${useRuntimeConfig().aghUrl}/control/${path}`, {
      method: call.method ?? 'GET',
      body: call.body,
      query: call.query,
      headers: authHeader(),
      timeout: call.timeout ?? 8000,
    })
    return res as T
  }
  catch (err) {
    const e = err as { statusCode?: number, data?: unknown }
    const status = e.statusCode ?? 0
    if (status >= 400 && status < 500 && status !== 401 && status !== 403) {
      const text = typeof e.data === 'string' ? e.data.trim() : ''
      throw createError({ statusCode: 400, statusMessage: 'agh_rejected', message: text || 'AdGuard отклонил запрос' })
    }
    useLogger().warn({ err, path }, 'agh request failed')
    throw createError({ statusCode: 502, statusMessage: 'agh_unavailable', message: 'AdGuard недоступен (нет соединения / неверные креды)' })
  }
}

/** Карточка AdGuard на Обзоре; недоступен - null. */
export async function fetchAdguardStats() {
  const data = await agh<AghStats>('stats', { timeout: 3000 }).catch(() => null)
  if (!data) return null
  const top = Object.entries(data.top_blocked_domains?.[0] ?? {})[0]
  return {
    totalToday: data.num_dns_queries ?? 0,
    blockedToday: data.num_blocked_filtering ?? 0,
    blockRatePct: data.num_dns_queries > 0 ? (data.num_blocked_filtering / data.num_dns_queries) * 100 : 0,
    avgMs: (data.avg_processing_time ?? 0) * 1000,
    topBlocked: top ? { domain: top[0], count: top[1] } : null,
  }
}

export const filteringStatus = () => agh<FilteringStatus>('filtering/status')
export const rewriteList = async () => (await agh<Rewrite[] | null>('rewrite/list')) ?? []
export const blockedServices = () => agh<{ ids: string[] | null, schedule?: Record<string, unknown> }>('blocked_services/get')

/** Состояние AdGuard после действия; не то, что просили, - 502, а не «готово». */
export async function confirmAgh<T>(state: T, ok: (st: T) => boolean, what: string): Promise<T> {
  if (!ok(state)) throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: `AdGuard не применил изменение: ${what}` })
  return state
}

/** Адрес в туннеле → имя клиента панели (по всем протоколам устройства). */
export async function panelNames(): Promise<Map<string, string>> {
  const rows = await useDb().select({ name: clients.name, wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip })
    .from(devices).innerJoin(clients, eq(devices.clientId, clients.id))
  const names = new Map<string, string>()
  for (const r of rows) {
    for (const ip of [r.wg, r.ovpn, r.ikev2]) if (ip) names.set(ip, r.name)
  }
  return names
}

/** id списка → название, чёрные и белые вместе. */
export const listNames = (st: FilteringStatus) => new Map([...(st.filters ?? []), ...(st.whitelist_filters ?? [])].map(f => [f.id, f.name]))
export const findList = (st: FilteringStatus, url: string, white: boolean) => ((white ? st.whitelist_filters : st.filters) ?? []).find(f => f.url === url)

export function listsView(st: FilteringStatus) {
  const row = (f: AghFilter) => ({ name: f.name, url: f.url, enabled: f.enabled, rules: f.rules_count, updated: f.last_updated ?? null })
  return { interval: st.interval, black: (st.filters ?? []).map(row), white: (st.whitelist_filters ?? []).map(row) }
}

let services: Array<{ id: string, name: string }> | null = null
/** Каталог сервисов меняется только с обновлением самого AdGuard. */
export async function serviceCatalog() {
  services ??= (await agh<{ blocked_services: Array<{ id: string, name: string }> }>('blocked_services/all')).blocked_services
  return services
}
export const serviceNames = async () => new Map((await serviceCatalog()).map(s => [s.id, s.name]))

/** check_host, пока ответ не станет нужным, но не дольше `ms`: после смены правил AdGuard пересобирает движок в фоне. */
export async function checkUntil(name: string, client: string | undefined, done: (r: CheckHost) => boolean, ms = 5000): Promise<CheckHost> {
  const until = Date.now() + ms
  for (;;) {
    const r = await agh<CheckHost>('filtering/check_host', { query: { name, client } })
    if (done(r) || Date.now() >= until) return r
    await new Promise(res => setTimeout(res, 150))
  }
}

/** Ответ ссылается на своё правило, которого уже нет: движок ещё старый. */
export const citesGone = (r: CheckHost, gone: string[]) => !!r.rules?.some(x => x.filter_list_id === 0 && gone.includes(x.text))

export const sameRewrite = (a: { domain: string, answer: string }, b: { domain: string, answer: string }) =>
  a.domain === b.domain && a.answer === b.answer

export async function findRewrite(domain: string, answer: string): Promise<Rewrite> {
  const cur = (await rewriteList()).find(x => sameRewrite(x, { domain, answer }))
  if (!cur) throw createError({ statusCode: 404, statusMessage: 'rewrite_not_found', message: `Перезаписи «${domain}» → ${answer} нет` })
  return cur
}

const HOST = /^(\*\.)?([a-z0-9_]([a-z0-9_-]*[a-z0-9_])?\.)*[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?$/i

/** Ошибка перезаписи словами или null: дубль и мусор AdGuard принимает молча. */
export function checkRewrite(domain: string, answer: string): string | null {
  if (!HOST.test(domain)) return `«${domain}» — не домен`
  if (answer === 'A' || answer === 'AAAA' || isIP(answer)) return null
  if (!HOST.test(answer) || answer.startsWith('*.')) return `Ответ «${answer}» — не IP-адрес и не домен`
  return null
}

interface DnsInfo {
  upstream_dns: string[] | null
  bootstrap_dns: string[] | null
  upstream_mode: string
  dnssec_enabled: boolean
  disable_ipv6: boolean
  cache_size: number
  cache_enabled?: boolean
  cache_optimistic: boolean
  ratelimit: number
  blocking_mode: string
  blocking_ipv4: string
  blocking_ipv6: string
}
interface RetentionConfig { interval: number, anonymize_client_ip?: boolean }

const HOUR = 3_600_000

function blockValue(i: Pick<DnsInfo, 'blocking_mode' | 'blocking_ipv4'>): string {
  switch (i.blocking_mode) {
    case 'nxdomain': return 'NXDOMAIN'
    case 'refused': return 'REFUSED'
    case 'custom_ip': return i.blocking_ipv4
    default: return '0.0.0.0'
  }
}

/** Ответ на блокировку из поля «Изменить» → поля dns_config или ошибка. */
export function parseBlock(v: string, cur: Pick<DnsInfo, 'blocking_mode' | 'blocking_ipv4' | 'blocking_ipv6'>):
  { set: Partial<DnsInfo> | null } | { error: string } {
  const s = v.trim()
  if (s === blockValue(cur)) return { set: null }
  const up = s.toUpperCase()
  if (up === 'NXDOMAIN') return { set: { blocking_mode: 'nxdomain' } }
  if (up === 'REFUSED') return { set: { blocking_mode: 'refused' } }
  if (s === '0.0.0.0') return { set: { blocking_mode: 'null_ip' } }
  if (isIP(s) === 4) return { set: { blocking_mode: 'custom_ip', blocking_ipv4: s, blocking_ipv6: isIP(cur.blocking_ipv6) === 6 ? cur.blocking_ipv6 : '::' } }
  return { error: 'Ответ на блокировку: IPv4-адрес, NXDOMAIN или REFUSED' }
}

/** «20 в секунду с клиента», «20», «без лимита» → число; 0 — без лимита. */
export function parseRatelimit(v: string): number | null {
  const s = v.trim().toLowerCase()
  if (s.startsWith('без')) return 0
  const m = s.match(/^(\d{1,6})(\s|$)/)
  return m ? Number(m[1]) : null
}

/** Bootstrap — только IP-адреса, через запятую или пробел. */
export function parseBootstrap(v: string): { list: string[] } | { error: string } {
  const list = v.split(/[\s,]+/).filter(Boolean)
  if (!list.length) return { error: 'Bootstrap: нужен хотя бы один IP-адрес' }
  const bad = list.find(x => !isIP(x.replace(/^\[(.*)\](:\d+)?$/, '$1').replace(/^([\d.]+):\d+$/, '$1')))
  return bad ? { error: `Bootstrap: «${bad}» — не IP-адрес` } : { list }
}

export const upstreamLines = (text: string) => text.split('\n').map(s => s.trim()).filter(Boolean)

/** Отказ AdGuard на серверы выше → по-русски, с номером строки. */
export function upstreamError(text: string): string {
  const m = text.match(/at index (\d+)/)
  return m ? `Строка ${Number(m[1]) + 1}: AdGuard не понял адрес сервера` : 'AdGuard не принял серверы выше'
}

/** Всё, из чего собирается раздел «Настройки», одним заходом. */
export async function settingsState() {
  const [info, sb, parental, ss, log, stats] = await Promise.all([
    agh<DnsInfo>('dns_info'),
    agh<{ enabled: boolean }>('safebrowsing/status'),
    agh<{ enabled: boolean }>('parental/status'),
    agh<{ enabled: boolean }>('safesearch/status'),
    agh<RetentionConfig>('querylog/config'),
    agh<RetentionConfig>('stats/config'),
  ])
  return { info, sb: sb.enabled, parental: parental.enabled, ss, log, stats }
}

function cacheText(i: DnsInfo): string {
  if (i.cache_enabled === false || !i.cache_size) return 'выключен'
  const mb = i.cache_size / 1024 / 1024
  return `${mb >= 10 || Number.isInteger(mb) ? Math.round(mb) : mb.toFixed(1).replace('.', ',')} МБ`
}

export function settingsView({ info: i, sb, parental, ss, log, stats }: Awaited<ReturnType<typeof settingsState>>) {
  const mode = i.upstream_mode === 'parallel' || i.upstream_mode === 'fastest_addr' ? i.upstream_mode : 'load_balance'
  return {
    upstreams: (i.upstream_dns ?? []).join('\n'),
    mode: mode as 'load_balance' | 'parallel' | 'fastest_addr',
    bootstrap: (i.bootstrap_dns ?? []).join(', '),
    safebrowsing: sb,
    parental,
    safesearch: ss.enabled,
    dnssec: i.dnssec_enabled,
    noAaaa: i.disable_ipv6,
    cache: cacheText(i),
    optimistic: i.cache_optimistic,
    ratelimit: i.ratelimit ? `${i.ratelimit} в секунду с клиента` : 'без лимита',
    block: blockValue(i),
    logHours: Math.round(log.interval / HOUR),
    statsHours: Math.round(stats.interval / HOUR),
    anonymize: !!log.anonymize_client_ip,
  }
}

/** Клиент панели глазами AdGuard: его клиент ищется по адресам в туннеле или по имени, без него - общие настройки. */
export function clientRows(panel: Array<{ name: string, tg: string | null, filter: boolean, ips: string[] }>,
  agh: AghPersistentClient[], counts: Map<string, number>, services: Map<string, string>) {
  return panel.map((c) => {
    const a = agh.find(x => x.ids.some(id => c.ips.includes(id))) ?? agh.find(x => x.name === c.name)
    const own = a && !a.use_global_settings
    const ss = a?.safe_search?.enabled ?? a?.safesearch_enabled ?? false
    return {
      name: c.name,
      tg: c.tg ? `@${c.tg.replace(/^@/, '')}` : 'без Telegram',
      ips: c.ips,
      filters: (!c.filter || (own && !a.filtering_enabled) ? 'off' : 'common') as 'common' | 'off',
      safesearch: (own ? (ss ? 'on' : 'off') : 'global') as 'global' | 'on' | 'off',
      services: a && !a.use_global_blocked_services
        ? (a.blocked_services ?? []).map(id => services.get(id) ?? id).join(', ') || 'ничего'
        : null,
      queries: c.ips.reduce((n, ip) => n + (counts.get(ip) ?? 0), 0),
    }
  })
}

type LogKind = 'ok' | 'blk' | 'rw' | 'safe'
const BLOCKED = new Set(['FilteredBlackList', 'FilteredBlockedService', 'FilteredInvalid'])
const REWRITTEN = new Set(['Rewrite', 'RewriteEtcHosts', 'RewriteRule', 'FilteredSafeSearch'])

const listName = (id: number | undefined, lists: Map<number, string>) => !id ? 'Свои правила' : lists.get(id) ?? `Список ${id}`
/** «tls://dns.google:853» → «dns.google». */
const upstreamHost = (u: string) => u.match(/^(?:[a-z0-9]+:\/\/)?(\[[^\]]+\]|[^/:]+)/i)?.[1] ?? u
/** Меньше 10 мс — с одним знаком, дальше целыми, запятая. */
const fmtMs = (ms: number) => String(ms < 10 ? Math.round(ms * 10) / 10 : Math.round(ms)).replace('.', ',')

function classify(e: AghLogEntry, lists: Map<number, string>, services: Map<string, string>): { kind: LogKind, detail: string } {
  if (e.reason === 'FilteredSafeBrowsing') return { kind: 'safe', detail: 'Безопасная навигация' }
  if (e.reason === 'FilteredParental') return { kind: 'safe', detail: 'Родительский контроль' }
  if (e.reason === 'FilteredBlockedService') return { kind: 'blk', detail: `Сервис: ${services.get(e.service_name ?? '') ?? e.service_name ?? ''}` }
  if (BLOCKED.has(e.reason)) return { kind: 'blk', detail: listName(e.rules?.[0]?.filter_list_id, lists) }
  if (REWRITTEN.has(e.reason)) return { kind: 'rw', detail: `→ ${(e.answer ?? []).find(x => x.value)?.value ?? 'пусто'}` }
  if (e.reason === 'NotFilteredWhiteList') return { kind: 'ok', detail: listName(e.rules?.[0]?.filter_list_id, lists) }
  if (e.cached) return { kind: 'ok', detail: 'из кэша' }
  const ms = Number(e.elapsedMs ?? 0)
  return { kind: 'ok', detail: e.upstream ? `${upstreamHost(e.upstream)} · ${fmtMs(ms)} мс` : `${fmtMs(ms)} мс` }
}

export function logRow(e: AghLogEntry, names: Map<string, string>, lists: Map<number, string>, services: Map<string, string>) {
  return {
    time: e.time,
    domain: e.question.name,
    type: e.question.type,
    ...classify(e, lists, services),
    client: names.get(e.client) ?? e.client_info?.name ?? e.client,
    ip: e.client,
  }
}

export function logMatch(r: ReturnType<typeof logRow>, f: 'all' | 'blk' | 'ok' | 'rw', q: string): boolean {
  if (f === 'blk' ? r.kind !== 'blk' && r.kind !== 'safe' : f !== 'all' && r.kind !== f) return false
  const s = q.toLowerCase()
  return !q || r.domain.toLowerCase().includes(s) || r.client.toLowerCase().includes(s) || r.ip.includes(s)
}

/** Блокировка из журнала: точное противоположное правило убирается, новое - отдельно, если без него не вышло. */
export function toggleRules(rules: string[], domain: string, block: boolean) {
  const opposite = block ? `@@||${domain}^` : `||${domain}^`
  const kept = rules.filter(r => r.trim() !== opposite)
  return { rules: kept, removed: kept.length !== rules.length, rule: block ? `||${domain}^` : `@@||${domain}^` }
}

const MODES: Record<string, string> = { '': 'балансировка', 'load_balance': 'балансировка', 'parallel': 'параллельно', 'fastest_addr': 'самый быстрый' }
const pair = (o: Record<string, number>): [string, number] => Object.entries(o)[0] ?? ['', 0]
const sum = (a: number[]) => a.reduce((x, y) => x + y, 0)

/**
 * Сводка: при почасовой статистике — последние сутки рядов AdGuard, при подневной — весь
 * интервал. Доля ответов из кэша и ход времени ответа — по последним записям журнала.
 */
export function summarize(s: AghStats, names: Map<string, string>, mode: string, recent: AghLogEntry[]) {
  const n = s.time_units === 'hours' ? 24 : s.dns_queries.length
  const last = (a: number[] | undefined) => (a ?? []).slice(-n)
  const q = last(s.dns_queries)
  const b = last(s.blocked_filtering)
  const sb = last(s.replaced_safebrowsing)
  const t = last(s.replaced_parental).map((v, i) => v + (sb[i] ?? 0))
  const queries = sum(q)
  const blocked = sum(b)

  const answered = recent.filter(e => e.reason.startsWith('NotFiltered'))
  const chrono = recent.filter(e => !e.cached && e.upstream).reverse()
  const step = Math.max(1, Math.ceil(chrono.length / 12))
  const ms: number[] = []
  for (let i = 0; i < chrono.length; i += step) {
    const part = chrono.slice(i, i + step)
    ms.push(sum(part.map(e => Number(e.elapsedMs ?? 0))) / part.length)
  }

  const total = sum(s.top_upstreams_responses.map(o => pair(o)[1]))
  const times = new Map(s.top_upstreams_avg_time.map(pair))
  const byName = new Map<string, number>()
  for (const [ip, c] of s.top_clients.map(pair)) {
    const name = names.get(ip) ?? ip
    byName.set(name, (byName.get(name) ?? 0) + c)
  }
  const top = (l: Top) => l.map(pair).slice(0, 7)

  return {
    units: s.time_units,
    queries,
    blocked,
    blockedPct: queries ? blocked / queries * 100 : 0,
    threats: sum(t),
    avgMs: s.avg_processing_time * 1000,
    cachedPct: answered.length ? Math.round(answered.filter(e => e.cached).length / answered.length * 100) : null,
    q,
    b,
    t,
    ms,
    upstreams: s.top_upstreams_responses.slice(0, 4).map((o) => {
      const [url, c] = pair(o)
      return { url, pct: total ? Math.round(c / total * 100) : 0, ms: (times.get(url) ?? 0) * 1000 }
    }),
    mode: MODES[mode] ?? mode,
    clients: [...byName].sort((x, y) => y[1] - x[1]).slice(0, 7),
    domains: top(s.top_queried_domains),
    blocks: top(s.top_blocked_domains),
  }
}

// Модификаторы DNS-правил AdGuard Home; у части без значения смысла нет.
const MODIFIERS = new Set(['important', 'client', 'ctag', 'dnstype', 'dnsrewrite', 'denyallow', 'badfilter'])
const NEED_VALUE = new Set(['client', 'ctag', 'dnstype', 'denyallow'])

/** Модификаторы через запятую; запятая в кавычках — часть значения. */
function splitModifiers(s: string): string[] {
  const out: string[] = []
  let cur = ''
  let q = ''
  for (const ch of s) {
    if (q) {
      if (ch === q) q = ''
    }
    else if (ch === '\'' || ch === '"') q = ch
    else if (ch === ',') {
      out.push(cur)
      cur = ''
      continue
    }
    cur += ch
  }
  out.push(cur)
  return out
}

/** Почему AdGuard молча пропустит строку; null - строка годная. */
function checkRule(raw: string): string | null {
  const r = raw.trim()
  if (!r || r.startsWith('!') || r.startsWith('#')) return null
  const hosts = r.split(/\s+/)
  if (hosts.length > 1 && isIP(hosts[0]!)) return null
  let body = r.startsWith('@@') ? r.slice(2) : r
  let mods: string | null = null
  if (body.startsWith('/')) {
    const end = body.lastIndexOf('/')
    if (end <= 0) return 'не закрыто регулярное выражение — нужен / в конце'
    try {
      new RegExp(body.slice(1, end))
    }
    catch {
      return 'регулярное выражение с ошибкой'
    }
    const tail = body.slice(end + 1)
    if (tail && !tail.startsWith('$')) return 'после регулярного выражения ждётся $ или конец строки'
    if (tail) mods = tail.slice(1)
  }
  else {
    const at = body.indexOf('$')
    if (at >= 0) {
      mods = body.slice(at + 1)
      body = body.slice(0, at)
    }
    if (/\s/.test(body)) return 'пробел внутри правила'
    if (!body.replace(/^\|\|?/, '').replace(/[\^|]+$/, '')) return 'нет адреса'
  }
  // `||x^$` AdGuard применяет как `||x^`.
  if (!mods?.trim()) return null
  for (const m of splitModifiers(mods)) {
    const [name = '', ...rest] = m.trim().replace(/^~/, '').split('=')
    if (!MODIFIERS.has(name)) return `неизвестный модификатор $${name}`
    const value = rest.join('=').trim()
    if (NEED_VALUE.has(name) && !value.replace(/^(['"])(.*)\1$/, '$2').trim()) return `у $${name} нет значения`
  }
  return null
}

/** Строки, которые AdGuard молча пропустит; номера строк — с единицы. */
export function checkRules(lines: string[]) {
  return lines.flatMap((text, i) => {
    const why = checkRule(text)
    return why ? [{ line: i + 1, text: text.trim(), why }] : []
  })
}

interface Verdict { kind: 'blk' | 'ok' | 'rw', label: string, why: [string, string, string] }

/** Ответ check_host словами: why - текст, правило моноширинным, текст. */
export function verdict(r: CheckHost, lists: Map<number, string>, services: Map<string, string>): Verdict {
  const rule = r.rules?.[0]
  const text = rule?.text ?? ''
  const id = rule?.filter_list_id ?? 0
  const list = lists.get(id) ?? `Список ${id}`
  switch (r.reason) {
    case 'FilteredBlackList':
      return { kind: 'blk', label: 'Заблокирован', why: ['Правило ', text, id === 0 ? ' из своих правил' : ` из списка «${list}»`] }
    case 'NotFilteredWhiteList':
      return { kind: 'ok', label: 'Разрешён', why: id === 0
        ? ['Правило ', text, ' разблокирует его, даже если он есть в списках']
        : ['Правило ', text, ` из белого списка «${list}»`] }
    case 'FilteredBlockedService':
      return { kind: 'blk', label: 'Заблокирован', why: [`Заблокирован сервис «${services.get(r.service_name ?? '') ?? r.service_name ?? ''}»`, '', ''] }
    case 'FilteredSafeBrowsing':
      return { kind: 'blk', label: 'Заблокирован', why: ['Опасный сайт: фишинг или вредонос', '', ''] }
    case 'FilteredParental':
      return { kind: 'blk', label: 'Заблокирован', why: ['Сайт для взрослых', '', ''] }
    case 'FilteredSafeSearch':
      return { kind: 'rw', label: 'Перезаписан', why: ['Безопасный поиск', '', ''] }
    case 'Rewrite':
    case 'RewriteEtcHosts':
      return { kind: 'rw', label: 'Перезаписан', why: ['Ответ ', r.ip_addrs?.[0] || r.cname || 'пусто', ' из перезаписей DNS'] }
    case 'RewriteRule':
      return { kind: 'rw', label: 'Перезаписан', why: ['Правило ', text, id === 0 ? ' из своих правил' : ` из списка «${list}»`] }
    default:
      return { kind: 'ok', label: 'Не фильтруется', why: ['Ни одно правило не подошло, запрос уйдёт на серверы выше', '', ''] }
  }
}
