/**
 * AllowedIPs для клиентских WireGuard-конфигов: весь интернет МИНУС локальные
 * диапазоны, чтобы домашняя/офисная сеть клиента (роутер, NAS, принтер, docker,
 * CGNAT мобильного оператора) не уезжала в туннель и оставалась доступной.
 *
 * WireGuard умеет только белый список — «исключить подсеть» в нём нет. Поэтому
 * `0.0.0.0/0` разворачивается в своё дополнение без:
 *
 *   0.0.0.0/8        this-network
 *   10.0.0.0/8       RFC1918
 *   100.64.0.0/10    CGNAT (сюда попадает 100.100.0.0/16)
 *   127.0.0.0/8      loopback
 *   172.16.0.0/12    RFC1918 (сюда попадает docker 172.17/172.18)
 *   192.168.0.0/16   RFC1918
 *   224.0.0.0/3      multicast + reserved — mDNS/Bonjour/SSDP, без них не
 *                    находятся Chromecast, AirPrint и сетевые шары
 *
 * 169.254.0.0/16 намеренно ОСТАВЛЕН в туннеле: его исключение стоит девяти
 * лишних префиксов, а APIPA у клиента и так on-link (более специфичный маршрут).
 *
 * Обратно внутрь туннеля пробиты сам wg-сегмент и mgmt-mesh (`tunnelNets`) —
 * там живёт DNS AdGuard (10.99.0.1). Без этого клиенты с filterTraffic=true
 * остаются без резолвера и «интернет пропадает по именам».
 *
 * IPv6 — не дополнение (там 133 префикса), а `2000::/3`: ровно всё глобально
 * маршрутизируемое, `fc00::/7` и `fe80::/10` отваливаются сами. Обратно
 * пробит `fd66:66::/64` (ULA fast-fail), иначе REJECT хаба до клиента не
 * доходит, см. `WG_ULA_NET`.
 *
 * Побочный эффект, ради которого это в том числе и делается: у WireGuard for
 * Windows `AllowedIPs = 0.0.0.0/0` включает kill-switch (block untunneled
 * traffic), который рубит локалку жёстко, мимо маршрутов. При сплите он
 * выключается сам.
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

const PUBLIC_V6 = '2000::/3'

/**
 * ULA fast-fail (см. wgUlaAddress в wireguard.ts): хаб отвечает на v6-пакет
 * REJECT'ом с адреса `fd66:66::1`, а не адреса назначения. Без этой дырки
 * в AllowedIPs клиент сам отбрасывает ответ на своей src-проверке, ICMP
 * до приложения не доходит, и дефект просто переезжает на обратный путь.
 */
const WG_ULA_NET = 'fd66:66::/64'

const FULL_TUNNEL = '0.0.0.0/0, ::/0'

/** `10.66.66.` → `10.66.66.0/24`. Пустой/битый префикс отбрасывается. */
function prefixToNet(prefix: string): string | null {
  const p = String(prefix || '').trim()
  return /^(\d{1,3}\.){3}$/.test(p) ? `${p}0/24` : null
}

const ipToInt = (ip: string): number =>
  ip.split('.').reduce((acc, o) => acc * 256 + Number(o), 0)

const intToIp = (n: number): string =>
  [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.')

function isIpv4(s: string): boolean {
  const o = s.split('.')
  return o.length === 4 && o.every(x => /^\d{1,3}$/.test(x) && Number(x) <= 255)
}

/**
 * Выбить из списка префиксов один хост — endpoint сервера.
 *
 * Зачем: при `AllowedIPs = 0.0.0.0/0` wg-quick заводит маршрут через fwmark +
 * `suppress_prefixlength`, и трафик до самого endpoint'а туннель не съедает.
 * Как только список становится не-/0, wg-quick кладёт обычные маршруты — и
 * если endpoint (193.233.245.247) попадает в свой же `193.0.0.0/8 dev wg0`,
 * получается петля: ядро пишет «Routing loop detected» и рубит пакеты, туннель
 * не поднимается вообще. Официальные клиенты Windows/Android/iOS от этого
 * защищены сами (bind сокета к физическому интерфейсу / VpnService.protect),
 * а вот Linux и macOS-CLI на wg-quick — нет.
 *
 * Содержащий endpoint префикс заменяется на его разбиение без этого /32:
 * `193.0.0.0/8` → 24 префикса. Дорого по символам, но иначе конфиг просто
 * не работает на половине клиентов.
 */
function excludeHost(cidrs: string[], host: string): string[] {
  if (!isIpv4(host)) return cidrs
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
 * Строка для `AllowedIPs =`.
 *  - splitLocal=false → классический full-tunnel `0.0.0.0/0, ::/0`
 *  - splitLocal=true  → интернет без локальных сетей + дырки в `tunnelPrefixes`,
 *    минус сам endpoint (см. `excludeHost`). Если endpoint задан именем, а не
 *    IP, дырку выбить не из чего — Linux-клиентам такого конфига грозит петля.
 */
export function wgAllowedIps(
  splitLocal: boolean,
  tunnelPrefixes: string[] = [],
  endpointHost = '',
): string {
  if (!splitLocal) return FULL_TUNNEL
  const nets = tunnelPrefixes.map(prefixToNet).filter(Boolean) as string[]
  return [...excludeHost(PUBLIC_V4, endpointHost), ...nets, PUBLIC_V6, WG_ULA_NET].join(', ')
}

/**
 * Клиентские `route`-директивы для .ovpn. У OpenVPN обратная логика: сервер
 * пушит `redirect-gateway def1` (две /1-половины), а более специфичные
 * маршруты через `net_gateway` возвращают локалку на физический интерфейс.
 * Порядок неважен — в таблице маршрутизации выигрывает длиннее префикс.
 *
 * `10.0.0.0/8` целиком уходит мимо туннеля, поэтому mgmt-сеть с DNS AdGuard
 * возвращается более специфичным `vpn_gateway` (тот же капкан, что и в WG).
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
