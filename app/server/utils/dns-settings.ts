import { isIP } from 'node:net'

// Перезаписи, клиенты и настройки AdGuard Home для вкладки DNS: перевод
// ответов /control/* в то, что показывает панель, и проверка ввода до отправки.
// AdGuard на ошибку отвечает по-английски или молча принимает дубль.

export interface Rewrite { domain: string, answer: string, enabled: boolean }

const HOST = /^(\*\.)?([a-z0-9_]([a-z0-9_-]*[a-z0-9_])?\.)*[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?$/i

/** Ошибка перезаписи словами или null. */
export function checkRewrite(domain: string, answer: string): string | null {
  if (!HOST.test(domain)) return `«${domain}» — не домен`
  if (answer === 'A' || answer === 'AAAA' || isIP(answer)) return null
  if (!HOST.test(answer) || answer.startsWith('*.')) return `Ответ «${answer}» — не IP-адрес и не домен`
  return null
}

export const sameRewrite = (a: { domain: string, answer: string }, b: { domain: string, answer: string }) =>
  a.domain === b.domain && a.answer === b.answer

type UpstreamMode = 'load_balance' | 'parallel' | 'fastest_addr'

export interface DnsInfo {
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

export interface SafeSearch { enabled: boolean, [engine: string]: boolean }
export interface LogConfig { enabled: boolean, interval: number, anonymize_client_ip: boolean, ignored: string[], ignored_enabled?: boolean }
export interface StatsConfig { enabled: boolean, interval: number, ignored: string[], ignored_enabled?: boolean }

export interface SettingsView {
  upstreams: string
  mode: UpstreamMode
  bootstrap: string
  safebrowsing: boolean
  parental: boolean
  safesearch: boolean
  dnssec: boolean
  noAaaa: boolean
  cache: string
  optimistic: boolean
  ratelimit: string
  block: string
  logHours: number
  statsHours: number
  anonymize: boolean
}

const HOUR = 3_600_000

/** Пустой режим AdGuard 0.107 — балансировка. */
const upstreamMode = (m: string): UpstreamMode => m === 'parallel' || m === 'fastest_addr' ? m : 'load_balance'

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

const ratelimitText = (n: number) => n ? `${n} в секунду с клиента` : 'без лимита'

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

/** Текст отказа AdGuard на серверы выше → по-русски, с номером строки. */
export function upstreamError(text: string): string {
  const m = text.match(/at index (\d+)/)
  return m ? `Строка ${Number(m[1]) + 1}: AdGuard не понял адрес сервера` : 'AdGuard не принял серверы выше'
}

function cacheText(i: Pick<DnsInfo, 'cache_size' | 'cache_enabled'>): string {
  if (i.cache_enabled === false || !i.cache_size) return 'выключен'
  const mb = i.cache_size / 1024 / 1024
  return `${mb >= 10 || Number.isInteger(mb) ? Math.round(mb) : mb.toFixed(1).replace('.', ',')} МБ`
}

export function settingsView(i: DnsInfo, sb: boolean, parental: boolean, ss: SafeSearch, log: LogConfig, stats: StatsConfig): SettingsView {
  return {
    upstreams: (i.upstream_dns ?? []).join('\n'),
    mode: upstreamMode(i.upstream_mode),
    bootstrap: (i.bootstrap_dns ?? []).join(', '),
    safebrowsing: sb,
    parental,
    safesearch: ss.enabled,
    dnssec: i.dnssec_enabled,
    noAaaa: i.disable_ipv6,
    cache: cacheText(i),
    optimistic: i.cache_optimistic,
    ratelimit: ratelimitText(i.ratelimit),
    block: blockValue(i),
    logHours: Math.round(log.interval / HOUR),
    statsHours: Math.round(stats.interval / HOUR),
    anonymize: log.anonymize_client_ip,
  }
}

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

export interface PanelClient { name: string, tg: string | null, filter: boolean, ips: string[] }

export interface DnsClientRow {
  name: string
  tg: string
  ips: string[]
  filters: 'common' | 'off'
  safesearch: 'global' | 'on' | 'off'
  services: string | null
  queries: number
}

/**
 * Клиент панели глазами AdGuard: его клиент в AdGuard ищется по адресам в
 * туннеле или по имени. Без своего клиента в AdGuard действуют общие настройки.
 */
export function clientRows(panel: PanelClient[], agh: AghPersistentClient[], counts: Map<string, number>, services: Map<string, string>): DnsClientRow[] {
  return panel.map((c) => {
    const a = agh.find(x => x.ids.some(id => c.ips.includes(id))) ?? agh.find(x => x.name === c.name)
    const own = a && !a.use_global_settings
    const ss = a?.safe_search?.enabled ?? a?.safesearch_enabled ?? false
    return {
      name: c.name,
      tg: c.tg ? `@${c.tg.replace(/^@/, '')}` : 'без Telegram',
      ips: c.ips,
      filters: !c.filter || (own && !a.filtering_enabled) ? 'off' : 'common',
      safesearch: own ? (ss ? 'on' : 'off') : 'global',
      services: a && !a.use_global_blocked_services
        ? (a.blocked_services ?? []).map(id => services.get(id) ?? id).join(', ') || 'ничего'
        : null,
      queries: c.ips.reduce((n, ip) => n + (counts.get(ip) ?? 0), 0),
    }
  })
}
