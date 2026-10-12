import { botClientView, clientByChat, requireBotAuth } from '../../../utils/bot-api'
import { deleteDevice } from '../../../utils/client-ops'

// Клиент удаляет свой девайс через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(deviceId) || !Number.isFinite(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }
  const client = await clientByChat(chatId)
  await deleteDevice(client.id, deviceId)

  return botClientView(client.id)
})
