import { desc, sql } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients, devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { clientStatus } from '../../utils/client-status'

/** Список клиентов: статус, число девайсов и суммарный трафик по девайсам. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const db = useDb()

  const clientRows = await db.select().from(clients).orderBy(desc(clients.createdAt))

  const agg = await db
    .select({
      clientId: devices.clientId,
      cnt: sql<number>`count(*)`,
      rx: sql<number>`coalesce(sum(${devices.rxTotal}), 0)`,
      tx: sql<number>`coalesce(sum(${devices.txTotal}), 0)`,
    })
    .from(devices)
    .groupBy(devices.clientId)
  const byClient = new Map(agg.map(a => [a.clientId, a]))

  return clientRows.map((c) => {
    const a = byClient.get(c.id)
    return {
      id: c.id,
      name: c.name,
      filterTraffic: c.filterTraffic,
      expiresAt: c.expiresAt,
      deviceLimit: c.deviceLimit,
      frozenManual: c.frozenManual,
      createdAt: c.createdAt,
      status: clientStatus(c),
      deviceCount: a ? Number(a.cnt) : 0,
      rxTotal: a ? Number(a.rx) : 0,
      txTotal: a ? Number(a.tx) : 0,
    }
  })
})
