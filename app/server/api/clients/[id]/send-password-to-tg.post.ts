import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { clients } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { botSecretOrThrow, sendBotEvent } from '../../../utils/bot-events'

/** Отправить пароль клиента в Telegram. Задел под будущую фичу. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  botSecretOrThrow()

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await sendBotEvent({ type: 'client_send_password', name: client.name, password: client.password })

  return { ok: true }
})
