import { count, eq } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { clients, devices } from '../../../../database/schema'
import { clientStatus } from '../../../../utils/client-status'
import { requireBotAuth } from '../../../../utils/bot-api'

/** Карточка клиента для админского чата бота. password — для диплинка-приглашения. */
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  const [c] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!c) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))

  return {
    id: c.id,
    name: c.name,
    status: clientStatus(c),
    frozenManual: c.frozenManual,
    filterTraffic: c.filterTraffic,
    deviceCount: Number(dc?.n ?? 0),
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    tgLinked: !!c.tgChatId,
    password: c.password,
  }
})
