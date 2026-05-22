import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { clients } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { notifyClient } from '../../../utils/bot-events'
import { generateClientPassword } from '../../../utils/password'

/**
 * Отвязать Telegram клиента от бота («Отозвать доступ к боту»).
 * Пароль перевыпускаем: инвайт-ссылка — это `?start=<password>`, и без
 * смены пароля старая ссылка позволила бы привязаться к боту заново.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  const [client] = await db.select({ tgChatId: clients.tgChatId }).from(clients)
    .where(eq(clients.id, id)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Уведомить ДО отвязки — после очистки tgChatId уже не отправить.
  if (client.tgChatId) {
    notifyClient(client.tgChatId, '🔒 Ваш доступ к этому боту отозван администратором.')
  }
  await db.update(clients)
    .set({
      tgChatId: null,
      tgUsername: null,
      password: generateClientPassword(),
      updatedAt: new Date(),
    })
    .where(eq(clients.id, id))

  return { ok: true }
})
