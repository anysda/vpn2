import { Resolver } from 'node:dns/promises'
import { isIP } from 'node:net'
import { domainToASCII, domainToUnicode } from 'node:url'
import { matchRoute } from './route-match'
import type { Conn, RoutePlan } from './route-match'
import type { ClashProxy } from './clash-client'

export interface CheckQuery { domain?: string, ip?: string, shown: string }

type CheckReason = 'manual' | 'ru' | 'youtube' | 'reject' | 'rule' | 'default'

export interface CheckResult {
  reason: CheckReason
  /** Что совпало: значение правила панели, суффикс или имя набора. */
  rule: string
  routeId: number | null
  /** Выход после раскрытия селекторов (pin-*, foreign-best, youtube-auto). */
  outbound: string
  delay: number | null
}

interface RouteRow { id: number, type: string, value: string, outbound: string }

/** Домен или IP из того, что вставили: ссылка, хост:порт, IDN. */
export function parseQuery(raw: string): CheckQuery | null {
  let q = raw.trim().toLowerCase().replace(/^[a-z][a-z0-9+.-]*:\/\//, '')
  q = (q.split(/[/?#]/)[0] ?? '').replace(/^[^@]*@/, '')
  const v6 = q.match(/^\[([^\]]+)\](?::\d+)?$/)
  if (v6) q = v6[1]!
  else if (!isIP(q)) q = q.replace(/:\d+$/, '')
  q = q.replace(/\.$/, '')
  if (isIP(q)) return { ip: q, shown: q }
  const ascii = domainToASCII(q)
  if (!ascii || !ascii.includes('.')) return null
  return { domain: ascii, shown: domainToUnicode(ascii) }
}

/**
 * Адрес, куда пойдёт клиент: тот же AdGuard без фильтров (127.0.0.2:53), что
 * отвечает клиентам VPN. Не ответил - адреса нет, правила по IP не совпадут.
 */
export async function resolveIp(domain: string, server = '127.0.0.2'): Promise<string | null> {
  const resolver = new Resolver({ timeout: 1500, tries: 1 })
  resolver.setServers([server])
  for (const ask of [() => resolver.resolve4(domain), () => resolver.resolve6(domain)]) {
    try {
      const [ip] = await ask()
      if (ip) return ip
    }
    catch { /* следующий тип записи */ }
  }
  return null
}

function unicodeSuffix(value: string) {
  if (!value.startsWith('.')) return domainToUnicode(value) || value
  return `.${domainToUnicode(value.slice(1)) || value.slice(1)}`
}

/** Соединение клиента VPN на 443/tcp: так панель моделирует обычный сайт. */
export function clientConn(q: CheckQuery, ip: string | null): Conn {
  return { inbound: 'wg-tproxy-in', network: 'tcp', protocol: 'tls', port: 443, domain: q.domain, ip: ip ?? q.ip }
}

export function explain(plan: RoutePlan, conn: Conn, routes: RouteRow[], proxies: ClashProxy[]): CheckResult {
  const hit = matchRoute(plan, conn)
  const tag = hit ? hit.outbound : plan.final
  const by = hit?.by
  let reason: CheckReason = 'default'
  let rule = ''
  let routeId: number | null = null
  if (hit) {
    if (by?.kind === 'ip_is_private') rule = by.kind
    else if (by) rule = by.sets.at(-1) && !by.sets[0]!.startsWith('manual-') ? by.sets.at(-1)! : unicodeSuffix(by.value)
    if (hit.action === 'reject') reason = 'reject'
    else if (by?.sets[0]?.startsWith('manual-')) {
      reason = 'manual'
      const out = tag.replace(/^pin-/, '')
      const row = routes.find(r => r.outbound === out
        && (r.type === 'domain' ? r.value.replace(/^\*\./, '') : r.value) === by.value)
      if (row) {
        rule = row.value
        routeId = row.id
      }
    }
    else if (tag.startsWith('youtube-')) reason = 'youtube'
    else if (tag === 'direct-ru') reason = 'ru'
    else reason = 'rule'
  }
  const byName = new Map(proxies.map(p => [p.name, p]))
  let outbound = tag
  const seen = new Set<string>()
  while (byName.get(outbound)?.now && !seen.has(outbound)) {
    seen.add(outbound)
    outbound = byName.get(outbound)!.now!
  }
  // Без clash закреплённый выход pin-X известен и так: это X.
  if (!byName.has(outbound)) outbound = outbound.replace(/^pin-/, '')
  const p = byName.get(outbound)
  const delay = p?.delay ?? p?.history?.at(-1)?.delay ?? null
  return { reason, rule, routeId, outbound, delay: delay || delay === 0 ? delay : null }
}
