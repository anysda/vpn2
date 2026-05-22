import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { reissueDeviceWg, syncWireguardConfig } from '../../../../../utils/wireguard'
import { reissueDeviceOvpn, syncOpenvpnConfig } from '../../../../../utils/openvpn'

/** Перевыпуск ключей одного девайса (кнопка на карточке девайса). */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const db = useDb()
  const [device] = await db.select({ id: devices.id }).from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await reissueDeviceWg(deviceId)
  await reissueDeviceOvpn(deviceId)
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device reissue failed'))
  await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after device reissue failed'))

  return { ok: true }
})
