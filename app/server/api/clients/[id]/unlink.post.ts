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
 *
 * Отзыв фиксируется первым, уведомление уходит после — на чат из строки до
 * очистки. Очередь бота может быть полна или бот не настроен: доступ всё
 * равно отозван, сбой доставки возвращается предупреждением.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const before = await useDb().transaction(async (tx) => {
    const [client] = await tx.select({ tgChatId: clients.tgChatId, name: clients.name }).from(clients)
      .where(eq(clients.id, id)).limit(1)
    if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })
    await tx.update(clients)
      .set({
        tgChatId: null,
        tgUsername: null,
        password: generateClientPassword(),
        updatedAt: new Date(),
      })
      .where(eq(clients.id, id))
    return client
  }, { behavior: 'immediate' })

  if (!before.tgChatId) return { ok: true }
  try {
    await notifyClient(before.tgChatId, '🔒 Ваш доступ к этому боту отозван администратором.', before.name)
    return { ok: true }
  }
  catch (err) {
    useLogger().warn({ err }, 'unlink notification not queued')
    const human = (err as { statusCode?: number }).statusCode && err instanceof Error
    return { ok: true, warning: human ? err.message : 'Уведомление клиенту не отправлено' }
  }
})
