import { eq } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { clients } from '../../../../database/schema'
import { requireAuth } from '../../../../utils/auth'
import { notifyClient } from '../../../../utils/bot-events'
import { deleteDevice } from '../../../../utils/client-ops'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const db = useDb()
  const device = await deleteDevice(clientId, deviceId)

  // Уведомить привязанного клиента — устройство удалил администратор.
  const [client] = await db.select({ tgChatId: clients.tgChatId }).from(clients)
    .where(eq(clients.id, clientId)).limit(1)
  if (client?.tgChatId) {
    notifyClient(
      client.tgChatId,
      `🗑 Администратор удалил ваше устройство «${device.name}» — его конфиг VPN больше не действует.`,
    )
  }

  return { ok: true }
})
