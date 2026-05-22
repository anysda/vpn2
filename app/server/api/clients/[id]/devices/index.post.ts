import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../../database/client'
import { clients, devices } from '../../../../database/schema'
import { requireAuth } from '../../../../utils/auth'
import { notifyClient } from '../../../../utils/bot-events'
import { ensureDeviceWg, syncWireguardConfig } from '../../../../utils/wireguard'
import { caReady, ensureDeviceOvpn } from '../../../../utils/openvpn'

const Body = z.object({ name: z.string().min(1).max(64) })

/** Добавить девайс клиенту. Сразу выдаёт ключи WG/OVPN. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(clientId)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const [client] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const name = body.name.trim()
  const existing = await db.select({ name: devices.name }).from(devices).where(eq(devices.clientId, clientId))
  if (existing.some(d => d.name === name)) {
    throw createError({ statusCode: 409, statusMessage: `Девайс «${name}» у клиента уже есть` })
  }
  if (client.deviceLimit != null && existing.length >= client.deviceLimit) {
    throw createError({ statusCode: 409, statusMessage: `Достигнут лимит девайсов (${client.deviceLimit})` })
  }

  const [device] = await db.insert(devices).values({ clientId, name }).returning()

  // Сразу минтим ключи, чтобы девайс был готов к выдаче конфигов.
  await ensureDeviceWg(device.id).catch(err => useLogger().error({ err }, 'ensureDeviceWg on add failed'))
  if (await caReady()) {
    await ensureDeviceOvpn(device.id).catch(err => useLogger().error({ err }, 'ensureDeviceOvpn on add failed'))
  }
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device add failed'))

  // Уведомить привязанного клиента — устройство добавил администратор.
  if (client.tgChatId) {
    notifyClient(
      client.tgChatId,
      `➕ Администратор добавил вам устройство «${name}». `
      + 'Откройте «📱 Мои устройства», чтобы получить конфиг.',
    )
  }

  return { id: device.id, clientId, name: device.name, createdAt: device.createdAt }
})
