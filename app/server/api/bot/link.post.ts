import { and, eq, ne } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { botClientView, requireBotAuth } from '../../utils/bot-api'

// Привязка Telegram клиента к аккаунту по паролю (диплинк ?start=<password>).
const Body = z.object({
  password: z.string().min(1),
  chatId: z.number().int(),
  username: z.string().optional(),
})

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const [client] = await db.select().from(clients)
    .where(eq(clients.password, body.password)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'invalid_password' })

  // Этот Telegram мог быть привязан к другому клиенту — отвязываем там.
  await db.update(clients).set({ tgChatId: null })
    .where(and(eq(clients.tgChatId, body.chatId), ne(clients.id, client.id)))
  await db.update(clients)
    .set({ tgChatId: body.chatId, tgUsername: body.username ?? null, updatedAt: new Date() })
    .where(eq(clients.id, client.id))

  return botClientView(client.id)
})
