import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { notifyClient } from '../../../../../utils/bot-events'
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
  const [device] = await db.select({ id: devices.id, name: devices.name }).from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await reissueDeviceWg(deviceId)
  await reissueDeviceOvpn(deviceId)
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device reissue failed'))
  await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after device reissue failed'))

  // Уведомить привязанного клиента — ключи перевыпустил администратор.
  const [client] = await db.select({ tgChatId: clients.tgChatId }).from(clients)
    .where(eq(clients.id, clientId)).limit(1)
  if (client?.tgChatId) {
    notifyClient(
      client.tgChatId,
      `🔄 Администратор перевыпустил ключи устройства «${device.name}». `
      + 'Старый конфиг больше не работает — скачайте новый в «📱 Мои устройства».',
    )
  }

  return { ok: true }
})
