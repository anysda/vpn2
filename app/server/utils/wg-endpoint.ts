import { isIP } from 'node:net'
import os from 'node:os'
import { createError } from 'h3'

/** Только публичный IPv4: адрес туннеля, docker или LAN не годится для автоопределения. */
function isPublicIpv4(address: string): boolean {
  if (isIP(address) !== 4) return false
  const [a, b] = address.split('.').map(Number) as [number, number, number, number]
  return a !== 0 && a !== 10 && a !== 127 && a < 224
    && !(a === 100 && b >= 64 && b <= 127)
    && !(a === 169 && b === 254)
    && !(a === 172 && b >= 16 && b <= 31)
    && !(a === 192 && b === 168)
}

/**
 * IPv6 endpoint попадает в split-local 2000::/3. Используем IPv4 входа;
 * при IPv6 в entry.host — единственный публичный IPv4 интерфейсов сервера
 * (панель разворачивается с --network host). За NAT нужен явный IPv4 входа.
 * Домены сохраняем; о наличии AAAA отдельно предупреждаем в панели.
 */
export function wgEndpoint(host: string): string {
  const literal = host.trim().replace(/^\[(.*)\]$/, '$1')
  if (isIP(literal) !== 6) return host

  const entryIp = (process.env.NUXT_WG_ENDPOINT_IP || '').trim()
  if (isIP(entryIp) === 4) return entryIp

  const addresses = new Set(Object.values(os.networkInterfaces()).flatMap(records =>
    (records || []).filter(r => !r.internal && r.family === 'IPv4' && isPublicIpv4(r.address))
      .map(r => r.address),
  ))
  if (addresses.size === 1) return [...addresses][0]!

  throw createError({
    statusCode: 422,
    statusMessage: 'wg_ipv4_endpoint_unavailable',
    message: 'Не удалось определить IPv4-адрес сервера для WireGuard. Укажите доступный клиентам IPv4 сервера в entry.public_host и примените конфигурацию.',
  })
}
