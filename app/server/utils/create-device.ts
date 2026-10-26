import { createHash } from 'node:crypto'
import { eq } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { clients, deviceRequests, devices } from '../database/schema'
import { nextPoolAddress } from './address-pools'

/**
 * Ключ идемпотентности создания: из заголовка idempotency-key, как у операций
 * доступа. hash — отпечаток тела: тот же ключ с другим запросом отклоняется.
 */
export function deviceRequestKey(event: H3Event, request: Record<string, unknown>) {
  const key = getHeader(event, 'idempotency-key')
  if (key === undefined) return undefined
  if (!/^[\w:.-]{1,128}$/.test(key)) throw createError({ statusCode: 400, message: 'Неверный ключ идемпотентности' })
  return { key, hash: createHash('sha256').update(JSON.stringify(request)).digest('hex') }
}

/**
 * Проверка имени и лимита, всех пулов и резервирование — одна write-транзакция,
 * до выпуска ключей: параллельные запросы видят устройства друг друга.
 * guard получает имена устройств клиента и его строку, прочитанные в этой же
 * транзакции (лимит, привязку бота), и бросает ошибку, если создавать нельзя.
 * С ключом идемпотентности повтор отдаёт уже созданное устройство (replay);
 * ключ живёт в device_requests и переживает устройство: повтор для чужого
 * клиента — 409, для удалённого или удаляемого устройства — 410.
 */
export async function createDeviceWithAddresses(
  clientId: number, name: string, enabled: { wg: boolean, ovpn: boolean },
  guard: (names: string[], client: typeof clients.$inferSelect) => void,
  idempotency?: { key: string, hash: string },
) {
  return useDb().transaction(async (tx) => {
    if (idempotency) {
      const [done] = await tx.select().from(deviceRequests).where(eq(deviceRequests.idempotencyKey, idempotency.key))
      if (done) {
        if (done.requestHash !== idempotency.hash || done.clientId !== clientId) {
          throw createError({ statusCode: 409, statusMessage: 'idempotency_conflict', message: 'Ключ идемпотентности уже использован для другого запроса' })
        }
        const [device] = await tx.select().from(devices).where(eq(devices.id, done.deviceId))
        if (!device || device.deleting) {
          throw createError({ statusCode: 410, statusMessage: 'device_deleted', message: 'Устройство по этому запросу уже удалено' })
        }
        return { device, replay: true }
      }
    }
    const [client] = await tx.select().from(clients).where(eq(clients.id, clientId))
    if (!client || client.deleting) throw createError({ statusCode: 409, message: 'Клиент удаляется' })
    const own = await tx.select({ name: devices.name }).from(devices).where(eq(devices.clientId, clientId))
    guard(own.map(d => d.name), client)
    const used = await tx.select({ wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip }).from(devices)
    const wgIp = enabled.wg ? nextPoolAddress('wg', used.map(r => r.wg)) : null
    const ovpnIp = enabled.ovpn ? nextPoolAddress('ovpn', used.map(r => r.ovpn)) : null
    const ikev2Ip = nextPoolAddress('ikev2', used.map(r => r.ikev2))
    const [device] = await tx.insert(devices).values({ clientId, name, wgIp, ovpnIp, ikev2Ip }).returning()
    if (!device) throw new Error('insert_failed')
    if (idempotency) {
      await tx.insert(deviceRequests).values({
        idempotencyKey: idempotency.key, requestHash: idempotency.hash, clientId, deviceId: device.id,
      })
    }
    return { device, replay: false }
  }, { behavior: 'immediate' })
}
