import { and, eq, ne } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { botClientView, requireBotAuth } from '../../utils/bot-api'
import { readBodyAs } from '../../utils/validate'

// Привязка Telegram клиента к аккаунту по паролю (диплинк ?start=<password>).
const Body = z.object({
  password: z.string().min(1),
  chatId: z.number().int(),
  username: z.string().optional(),
})

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readBodyAs(event, Body)

  // Пароль перепроверяется тем же UPDATE, что пишет привязку: отзыв между
  // поиском и записью меняет пароль, и запись не находит строку.
  const db = useDb()
  const [found] = await db.select({ id: clients.id }).from(clients)
    .where(eq(clients.password, body.password)).limit(1)
  if (!found) throw createError({ statusCode: 404, statusMessage: 'invalid_password' })
  const clientId = await db.transaction(async (tx) => {
    const [linked] = await tx.update(clients)
      .set({ tgChatId: body.chatId, tgUsername: body.username ?? null, updatedAt: new Date() })
      .where(and(eq(clients.id, found.id), eq(clients.password, body.password)))
      .returning({ id: clients.id })
    if (!linked) throw createError({ statusCode: 404, statusMessage: 'invalid_password' })
    // Этот Telegram мог быть привязан к другому клиенту — отвязываем там.
    await tx.update(clients).set({ tgChatId: null })
      .where(and(eq(clients.tgChatId, body.chatId), ne(clients.id, linked.id)))
    return linked.id
  }, { behavior: 'immediate' })

  return botClientView(clientId)
})
