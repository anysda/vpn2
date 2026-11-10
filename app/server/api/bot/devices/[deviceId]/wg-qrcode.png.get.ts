import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { devices } from '../../../../database/schema'
import { clientByChat, requireBotAuth, requireChatLinked } from '../../../../utils/bot-api'
import { qrPng } from '../../../../utils/qr'
import { deviceWgConfig, requireWgEndpoint } from '../../../../utils/wg-device-config'

// QR WireGuard-конфига девайса для бота: тот же генератор, что SVG панели.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(deviceId) || !Number.isFinite(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }

  const endpoint = requireWgEndpoint()

  const client = await clientByChat(chatId)
  const db = useDb()
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, client.id)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const png = qrPng(await deviceWgConfig(deviceId, client.filterTraffic, endpoint))

  // Последний await перед ответом: отвязанный за время сборки чат QR не получает.
  await requireChatLinked(client.id, chatId)
  setHeader(event, 'content-type', 'image/png')
  return png
})
