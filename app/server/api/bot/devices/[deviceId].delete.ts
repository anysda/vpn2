import { botClientView, clientByChat, requireBotAuth } from '../../../utils/bot-api'
import { accessRequestKey, enqueueAccess } from '../../../utils/access-operations'

// Клиент удаляет свой девайс через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(deviceId) || !Number.isFinite(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }
  const client = await clientByChat(chatId)
  const { operation } = await enqueueAccess({ clientId: client.id, chatId, deviceId, kind: 'delete', actor: 'bot', key: accessRequestKey(event) })
  setResponseStatus(event, 202)
  return { ...await botClientView(client.id), operation }
})
