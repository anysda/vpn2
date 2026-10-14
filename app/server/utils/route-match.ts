import { BlockList, isIP } from 'node:net'

// Снимок маршрутизации, который роутер пишет вместе с принятым config.json
// (anysda-apply-routes.py): route.rules, route.final и содержимое наборов.
type SbRule = Record<string, unknown>
export interface RoutePlan {
  rules: SbRule[]
  final: string
  rule_set: Record<string, SbRule[]>
}

export interface Conn {
  inbound: string
  network: 'tcp' | 'udp'
  protocol: string
  port: number
  domain?: string
  ip?: string
  /** VPN-адрес устройства клиента: по нему совпадают client-pref-* и lane-*. */
  source?: string
}

/** Что именно совпало: элемент правила и набор, в котором он лежит. */
interface MatchedBy { kind: string, value: string, sets: string[] }

export interface RouteHit {
  index: number
  action: string
  outbound: string
  by: MatchedBy | null
}

// Ключи, которые задают действие, а не условие.
const ACTION_KEYS = new Set([
  'action', 'outbound', 'invert', 'type', 'mode', 'rules',
  'override_address', 'override_port', 'network_strategy', 'network_type',
  'fallback_network_type', 'fallback_delay', 'udp_disable_domain_unmapping',
  'udp_connect', 'udp_timeout', 'tls_fragment', 'tls_fragment_fallback_delay',
  'tls_record_fragment', 'method', 'no_drop', 'sniffer', 'timeout', 'strategy',
  'server', 'disable_cache', 'rewrite_ttl', 'client_subnet',
])
// Условия sing-box объединяет группами: внутри группы - ИЛИ, группы и
// остальные условия - И.
const DEST = new Set(['domain', 'domain_suffix', 'domain_keyword', 'domain_regex', 'ip_cidr', 'ip_is_private', 'rule_set'])
const SOURCE = new Set(['source_ip_cidr', 'source_ip_is_private'])
const PORT = new Set(['port', 'port_range'])
const SOURCE_PORT = new Set(['source_port', 'source_port_range'])
// Не дают перехода к следующему правилу только эти действия.
const NON_FINAL = new Set(['sniff', 'resolve', 'route-options'])

const PRIVATE: Array<[string, number, 'ipv4' | 'ipv6']> = [
  ['0.0.0.0', 8, 'ipv4'], ['10.0.0.0', 8, 'ipv4'], ['100.64.0.0', 10, 'ipv4'],
  ['127.0.0.0', 8, 'ipv4'], ['169.254.0.0', 16, 'ipv4'], ['172.16.0.0', 12, 'ipv4'],
  ['192.168.0.0', 16, 'ipv4'], ['224.0.0.0', 4, 'ipv4'], ['240.0.0.0', 4, 'ipv4'],
  ['::', 128, 'ipv6'], ['::1', 128, 'ipv6'], ['fc00::', 7, 'ipv6'], ['fe80::', 10, 'ipv6'], ['ff00::', 8, 'ipv6'],
]
let privateList: BlockList | null = null

function list(value: unknown): string[] {
  if (value === undefined || value === null) return []
  return (Array.isArray(value) ? value : [value]).map(String)
}

const cidrCache = new WeakMap<SbRule, BlockList>()
const sourceCache = new WeakMap<SbRule, BlockList>()
function cidrs(rule: SbRule, key: 'ip_cidr' | 'source_ip_cidr' = 'ip_cidr'): BlockList {
  const cache = key === 'ip_cidr' ? cidrCache : sourceCache
  let bl = cache.get(rule)
  if (!bl) {
    bl = new BlockList()
    for (const c of list(rule[key])) {
      const [addr = '', bits] = c.split('/')
      const family = isIP(addr)
      if (!family) continue
      const type = family === 4 ? 'ipv4' : 'ipv6'
      bl.addSubnet(addr, bits === undefined ? (family === 4 ? 32 : 128) : Number(bits), type)
    }
    cache.set(rule, bl)
  }
  return bl
}

function ipIn(bl: BlockList, ip: string) {
  return bl.check(ip, isIP(ip) === 4 ? 'ipv4' : 'ipv6')
}

function isPrivate(ip: string) {
  if (!privateList) {
    privateList = new BlockList()
    for (const [a, b, t] of PRIVATE) privateList.addSubnet(a, b, t)
  }
  return ipIn(privateList, ip)
}

function suffixHit(domain: string, suffix: string) {
  return suffix.startsWith('.') ? domain.endsWith(suffix) : domain === suffix || domain.endsWith(`.${suffix}`)
}

function regexHit(domain: string, re: string) {
  try {
    return new RegExp(re).test(domain)
  }
  catch {
    return false
  }
}

function portHit(conn: Conn, rule: SbRule, key: string) {
  if (key === 'port') return list(rule.port).some(p => Number(p) === conn.port)
  return list(rule.port_range).some((r) => {
    const [from, to] = r.split(':')
    return (from === '' || conn.port >= Number(from)) && (to === '' || to === undefined || conn.port <= Number(to))
  })
}

/** Первый совпавший элемент группы адреса назначения или null. */
function destHit(plan: RoutePlan, conn: Conn, rule: SbRule, key: string): MatchedBy | null {
  const d = conn.domain
  const ip = conn.ip
  switch (key) {
    case 'domain': {
      const v = d ? list(rule.domain).find(x => x === d) : undefined
      return v ? { kind: key, value: v, sets: [] } : null
    }
    case 'domain_suffix': {
      const v = d ? list(rule.domain_suffix).find(x => suffixHit(d, x)) : undefined
      return v ? { kind: key, value: v, sets: [] } : null
    }
    case 'domain_keyword': {
      const v = d ? list(rule.domain_keyword).find(x => d.includes(x)) : undefined
      return v ? { kind: key, value: v, sets: [] } : null
    }
    case 'domain_regex': {
      const v = d ? list(rule.domain_regex).find(x => regexHit(d, x)) : undefined
      return v ? { kind: key, value: v, sets: [] } : null
    }
    case 'ip_cidr': {
      if (!ip || !ipIn(cidrs(rule), ip)) return null
      // Какая именно подсеть совпала - для подписи и поиска правила панели;
      // в больших наборах (geoip) подписью остаётся сам адрес.
      const all = list(rule.ip_cidr)
      const v = all.length <= 64 ? all.find(c => ipIn(cidrs({ ip_cidr: c }), ip)) : undefined
      return { kind: key, value: v ?? ip, sets: [] }
    }
    case 'ip_is_private':
      return rule.ip_is_private === true && ip && isPrivate(ip) ? { kind: key, value: ip, sets: [] } : null
    case 'rule_set':
      for (const tag of list(rule.rule_set)) {
        for (const inner of plan.rule_set[tag] ?? []) {
          const by = ruleHit(plan, conn, inner)
          if (by) return { kind: by.kind, value: by.value, sets: [tag, ...by.sets] }
        }
      }
      return null
  }
  return null
}

/** Группа условий по адресу источника: без адреса клиента не совпадает. */
function sourceHit(conn: Conn, rule: SbRule, keys: string[]): MatchedBy | null {
  const src = conn.source
  if (!src) return null
  for (const k of keys) {
    if (k === 'source_ip_cidr' && ipIn(cidrs(rule, k), src)) return { kind: k, value: src, sets: [] }
    if (k === 'source_ip_is_private' && rule.source_ip_is_private === true && isPrivate(src)) return { kind: k, value: src, sets: [] }
  }
  return null
}

function otherHit(conn: Conn, rule: SbRule, key: string): boolean {
  switch (key) {
    case 'inbound': return list(rule.inbound).includes(conn.inbound)
    case 'network': return list(rule.network).includes(conn.network)
    case 'protocol': return list(rule.protocol).includes(conn.protocol)
    case 'ip_version': return !!conn.ip && Number(rule.ip_version) === isIP(conn.ip)
  }
  // Условие, которое панель не умеет проверить, не считаем совпавшим.
  return false
}

/**
 * Совпадает ли правило (обычное или logical) с соединением. Возвращает, чем
 * совпало, или null. Для правил без условий по адресу - пустое MatchedBy.
 */
function ruleHit(plan: RoutePlan, conn: Conn, rule: SbRule): MatchedBy | null {
  const invert = rule.invert === true
  let hit: MatchedBy | null
  if (rule.type === 'logical') {
    const subs = Array.isArray(rule.rules) ? rule.rules as SbRule[] : []
    const results = subs.map(r => ruleHit(plan, conn, r))
    const ok = rule.mode === 'or' ? results.some(Boolean) : results.length > 0 && results.every(Boolean)
    const sub = subs.findIndex((r, i) => results[i] && r.invert !== true)
    hit = ok ? (sub >= 0 ? results[sub]! : { kind: '', value: '', sets: [] }) : null
  }
  else {
    const keys = Object.keys(rule).filter(k => !ACTION_KEYS.has(k))
    let by: MatchedBy | null = null
    let ok = true
    const dest = keys.filter(k => DEST.has(k))
    if (dest.length) {
      for (const k of dest) {
        by = destHit(plan, conn, rule, k)
        if (by) break
      }
      ok = !!by
    }
    if (ok && keys.some(k => SOURCE_PORT.has(k))) ok = false
    const source = keys.filter(k => SOURCE.has(k))
    if (ok && source.length) {
      const src = sourceHit(conn, rule, source)
      ok = !!src
      by ??= src
    }
    const ports = keys.filter(k => PORT.has(k))
    if (ok && ports.length) ok = ports.some(k => portHit(conn, rule, k))
    if (ok) ok = keys.filter(k => !DEST.has(k) && !PORT.has(k) && !SOURCE.has(k)).every(k => otherHit(conn, rule, k))
    hit = ok ? (by ?? { kind: '', value: '', sets: [] }) : null
  }
  if (invert) return hit ? null : { kind: '', value: '', sets: [] }
  return hit
}

/** Первое итоговое правило route.rules, как его выбрал бы sing-box; null - route.final. */
export function matchRoute(plan: RoutePlan, conn: Conn): RouteHit | null {
  for (const [index, rule] of plan.rules.entries()) {
    const action = String(rule.action ?? 'route')
    if (NON_FINAL.has(action)) continue
    const by = ruleHit(plan, conn, rule)
    if (by) return { index, action, outbound: String(rule.outbound ?? ''), by: by.kind ? by : null }
  }
  return null
}
