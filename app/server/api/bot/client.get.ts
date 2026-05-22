import { botClientView, clientByChat, requireBotAuth } from '../../utils/bot-api'

// Резолв клиента по Telegram-чату — бот зовёт на каждое взаимодействие.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(chatId)) throw createError({ statusCode: 400, statusMessage: 'invalid_chat_id' })
  const client = await clientByChat(chatId)
  return botClientView(client.id)
})
