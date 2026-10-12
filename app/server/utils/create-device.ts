import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { devices } from '../database/schema'
import { nextPoolAddress } from './address-pools'

/**
 * Проверка имени и лимита, всех пулов и резервирование — одна write-транзакция,
 * до выпуска ключей: параллельные запросы видят устройства друг друга.
 * guard получает имена устройств клиента и бросает ошибку, если создавать нельзя.
 */
export async function createDeviceWithAddresses(
  clientId: number, name: string, enabled: { wg: boolean, ovpn: boolean },
  guard: (names: string[]) => void,
) {
  return useDb().transaction(async (tx) => {
    const own = await tx.select({ name: devices.name }).from(devices).where(eq(devices.clientId, clientId))
    guard(own.map(d => d.name))
    const used = await tx.select({ wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip }).from(devices)
    const wgIp = enabled.wg ? nextPoolAddress('wg', used.map(r => r.wg)) : null
    const ovpnIp = enabled.ovpn ? nextPoolAddress('ovpn', used.map(r => r.ovpn)) : null
    const ikev2Ip = nextPoolAddress('ikev2', used.map(r => r.ikev2))
    const [device] = await tx.insert(devices).values({ clientId, name, wgIp, ovpnIp, ikev2Ip }).returning()
    if (!device) throw new Error('insert_failed')
    return device
  }, { behavior: 'immediate' })
}
