import { createError } from 'h3'
import pools from '../../shared/vpn-pools.json' with { type: 'json' }

type Protocol = 'wg' | 'ovpn' | 'ikev2'

/** Старые адреса идут первыми; .0/.1/.255 и резерв OpenVPN не выдаются. */
export function nextPoolAddress(protocol: Protocol, usedIps: Array<string | null | undefined>): string {
  const pool = pools[protocol]
  const used = new Set(usedIps)
  let count = 0
  for (const [index, subnet] of pool.subnets.entries()) {
    const prefix = subnet.slice(0, subnet.lastIndexOf('.') + 1)
    for (let host = 2; host <= (index === 0 ? pool.legacyLast : 254) && count < pools.capacity; host++, count++) {
      const ip = `${prefix}${host}`
      if (!used.has(ip)) return ip
    }
  }
  throw createError({
    statusCode: 409,
    statusMessage: `${protocol}_pool_exhausted`,
    message: `Нет свободных адресов ${pool.name}. Удалите неиспользуемое устройство или обратитесь к администратору`,
  })
}
