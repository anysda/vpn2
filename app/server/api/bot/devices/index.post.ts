import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../database/client'
import { devices } from '../../../database/schema'
import { botClientView, clientByChat, requireBotAuth } from '../../../utils/bot-api'
import { ensureDeviceWg, syncWireguardConfig } from '../../../utils/wireguard'
import { caReady, ensureDeviceOvpn } from '../../../utils/openvpn'

const Body = z.object({
  chatId: z.number().int(),
  name: z.string().min(1).max(64),
})

// Клиент добавляет себе девайс через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const client = await clientByChat(body.chatId)
  const db = useDb()

  const name = body.name.trim()
  const existing = await db.select({ name: devices.name }).from(devices)
    .where(eq(devices.clientId, client.id))
  if (existing.some(d => d.name === name)) {
    throw createError({ statusCode: 409, statusMessage: 'Девайс с таким названием уже есть' })
  }
  if (client.deviceLimit != null && existing.length >= client.deviceLimit) {
    throw createError({ statusCode: 409, statusMessage: 'Достигнут лимит устройств — обратитесь к администратору' })
  }

  const [device] = await db.insert(devices).values({ clientId: client.id, name }).returning()
  await ensureDeviceWg(device.id).catch(err => useLogger().error({ err }, 'bot: ensureDeviceWg failed'))
  if (await caReady()) {
    await ensureDeviceOvpn(device.id).catch(err => useLogger().error({ err }, 'bot: ensureDeviceOvpn failed'))
  }
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'bot: wg sync failed'))

  return botClientView(client.id)
})
