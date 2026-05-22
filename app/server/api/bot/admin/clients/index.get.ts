import { desc, sql } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { clients, devices } from '../../../../database/schema'
import { clientStatus } from '../../../../utils/client-status'
import { requireBotAuth } from '../../../../utils/bot-api'

/** Список клиентов для админского управления из бота. */
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const db = useDb()

  const rows = await db.select().from(clients).orderBy(desc(clients.createdAt))
  const agg = await db
    .select({ clientId: devices.clientId, cnt: sql<number>`count(*)` })
    .from(devices)
    .groupBy(devices.clientId)
  const byClient = new Map(agg.map(a => [a.clientId, Number(a.cnt)]))

  return rows.map(c => ({
    id: c.id,
    name: c.name,
    status: clientStatus(c),
    deviceCount: byClient.get(c.id) ?? 0,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    tgLinked: !!c.tgChatId,
  }))
})
