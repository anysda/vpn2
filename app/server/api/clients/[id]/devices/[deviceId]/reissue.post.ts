import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { actorOf, joinDetail, recordEvent } from '../../../../../utils/events'
import { notifyClient } from '../../../../../utils/bot-events'
import { reissueDeviceWg, syncWireguardConfig } from '../../../../../utils/wireguard'
import { reissueDeviceOvpn, syncOpenvpnConfig } from '../../../../../utils/openvpn'
import { reissueDeviceIkev2, syncIkev2 } from '../../../../../utils/ikev2'

/** Перевыпуск ключей одного девайса (кнопка на карточке девайса). */
export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
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

  await reissueDeviceOvpn(deviceId)
  await reissueDeviceWg(deviceId)
  await reissueDeviceIkev2(deviceId)
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device reissue failed'))
  await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after device reissue failed'))
  await syncIkev2().catch(err => useLogger().error({ err }, 'ikev2 sync after device reissue failed'))

  // Уведомить привязанного клиента — ключи перевыпустил администратор.
  const [client] = await db.select({ tgChatId: clients.tgChatId, name: clients.name }).from(clients)
    .where(eq(clients.id, clientId)).limit(1)
  await recordEvent({
    type: 'client_reissued', level: 'info', object: `client:${clientId}`, actor: actorOf(user),
    title: `Ключи перевыпущены · ${client?.name ?? device.name}`, detail: joinDetail(`устройство «${device.name}»`, actorOf(user)),
  })
  if (client?.tgChatId) {
    await notifyClient(
      client.tgChatId,
      `🔄 Администратор перевыпустил ключи устройства «${device.name}». `
      + 'Старый конфиг больше не работает — скачайте новый в «📱 Мои устройства».',
      client.name,
    )
  }

  return { ok: true }
})
