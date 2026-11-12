import { isIP } from 'node:net'

/**
 * AllowedIPs клиентских WireGuard-конфигов: интернет без локальных диапазонов
 * (RFC1918, CGNAT, loopback, multicast), чтобы домашняя сеть клиента не уезжала
 * в туннель. WireGuard знает только белый список, поэтому здесь дополнение
 * `0.0.0.0/0`; 169.254/16 оставлен: он у клиента и так on-link. Заодно у
 * WireGuard for Windows не включается kill-switch, который рубит локалку.
 */
const PUBLIC_V4 = [
  '1.0.0.0/8', '2.0.0.0/7', '4.0.0.0/6', '8.0.0.0/7', '11.0.0.0/8', '12.0.0.0/6',
  '16.0.0.0/4', '32.0.0.0/3', '64.0.0.0/3', '96.0.0.0/6', '100.0.0.0/10',
  '100.128.0.0/9', '101.0.0.0/8', '102.0.0.0/7', '104.0.0.0/5', '112.0.0.0/5',
  '120.0.0.0/6', '124.0.0.0/7', '126.0.0.0/8', '128.0.0.0/3', '160.0.0.0/5',
  '168.0.0.0/6', '172.0.0.0/12', '172.32.0.0/11', '172.64.0.0/10', '172.128.0.0/9',
  '173.0.0.0/8', '174.0.0.0/7', '176.0.0.0/4', '192.0.0.0/9', '192.128.0.0/11',
  '192.160.0.0/13', '192.169.0.0/16', '192.170.0.0/15', '192.172.0.0/14',
  '192.176.0.0/12', '192.192.0.0/10', '193.0.0.0/8', '194.0.0.0/7', '196.0.0.0/6',
  '200.0.0.0/5', '208.0.0.0/4',
]

/** IPv6: всё глобально маршрутизируемое и ULA хаба, с которого приходит REJECT (см. wgUlaAddress). */
const PUBLIC_V6 = '2000::/3, fd66:66::/64'

/** `10.66.66.` → `10.66.66.0/24`; битый префикс отбрасывается. */
function prefixToNet(prefix: string): string | null {
  const p = String(prefix || '').trim()
  return /^(\d{1,3}\.){3}$/.test(p) ? `${p}0/24` : null
}

const ipToInt = (ip: string): number => ip.split('.').reduce((acc, o) => acc * 256 + Number(o), 0)
const intToIp = (n: number): string => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.')

/**
 * Выбивает из префиксов endpoint сервера: при списке не-/0 wg-quick кладёт
 * обычные маршруты, и endpoint внутри своего же префикса даёт петлю (Linux и
 * macOS-CLI). Префикс заменяется разбиением без этого /32.
 */
function excludeHost(cidrs: string[], host: string): string[] {
  if (isIP(host) !== 4) return cidrs
  const ip = ipToInt(host)
  const out: string[] = []
  for (const cidr of cidrs) {
    const [base, lenStr] = cidr.split('/')
    const len = Number(lenStr)
    const start = ipToInt(base!)
    const size = 2 ** (32 - len)
    if (ip < start || ip >= start + size) {
      out.push(cidr)
      continue
    }
    // Спускаемся по дереву к /32 с host'ом, забирая на каждом шаге вторую половину.
    const halves: string[] = []
    let cur = start
    for (let l = len; l < 32; l++) {
      const half = 2 ** (32 - (l + 1))
      const goLeft = ip < cur + half
      halves.push(`${intToIp(goLeft ? cur + half : cur)}/${l + 1}`)
      if (!goLeft) cur += half
    }
    out.push(...halves.sort((a, b) => ipToInt(a.split('/')[0]!) - ipToInt(b.split('/')[0]!)))
  }
  return out
}

/**
 * Строка для `AllowedIPs =`. Без splitLocal классический full-tunnel; иначе
 * интернет без локальных сетей, с `tunnelPrefixes` и без endpoint. Если endpoint
 * задан именем, вычитается `endpointIp`.
 */
export function wgAllowedIps(splitLocal: boolean, tunnelPrefixes: string[] = [], endpointHost = '', endpointIp = ''): string {
  if (!splitLocal) return '0.0.0.0/0, ::/0'
  const nets = tunnelPrefixes.map(prefixToNet).filter(Boolean) as string[]
  const host = isIP(endpointHost) === 4 ? endpointHost : endpointIp.trim()
  return [...excludeHost(PUBLIC_V4, host), ...nets, PUBLIC_V6].join(', ')
}

/**
 * `route`-директивы .ovpn: сервер пушит redirect-gateway, а более специфичные
 * маршруты через net_gateway возвращают локалку. mgmt-сеть с DNS AdGuard
 * возвращается в туннель через vpn_gateway.
 */
export function ovpnLocalRoutes(splitLocal: boolean, tunnelPrefixes: string[] = []): string[] {
  if (!splitLocal) return []
  const lines = [
    '# Локальные сети — мимо туннеля, на физический интерфейс.',
    'route 10.0.0.0 255.0.0.0 net_gateway',
    'route 100.64.0.0 255.192.0.0 net_gateway',
    'route 172.16.0.0 255.240.0.0 net_gateway',
    'route 192.168.0.0 255.255.0.0 net_gateway',
  ]
  for (const prefix of tunnelPrefixes) {
    const net = prefixToNet(prefix)
    if (net) lines.push(`route ${net.replace('/24', '')} 255.255.255.0 vpn_gateway`)
  }
  return lines
}
