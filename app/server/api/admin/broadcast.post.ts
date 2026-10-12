import { isNotNull } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { notifyBot } from '../../utils/bot-events'
import { readBodyAs } from '../../utils/validate'

const Body = z.object({ text: z.string().trim().min(1).max(4096) })

/** Объявление от имени бота всем клиентам, привязанным к Telegram. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { text } = await readBodyAs(event, Body)

  const rows = await useDb()
    .select({ chatId: clients.tgChatId })
    .from(clients)
    .where(isNotNull(clients.tgChatId))

  const chatIds = [...new Set(rows.map(row => row.chatId!))]
  const delivery = chatIds.length ? await notifyBot('broadcast', { chatIds, text }, 'Рассылка') : null
  return { recipients: chatIds.length, delivery }
})
