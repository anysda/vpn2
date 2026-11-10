import { eq, sql } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { accessStatus } from '../../shared/access-operation'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { currentAccessByClient, latestAccess } from './access-operations'
import { clientStatus } from './client-status'
import { clientsNewestFirst, devicesOldestFirst } from './list-order'

const UNCONFIRMED = 'Состояние не подтверждено'

/** Список клиентов для панели; бот берёт из него свои поля. */
export async function clientList() {
  const db = useDb()
  const rows = await db.select().from(clients).orderBy(...clientsNewestFirst)
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
  const operations = await currentAccessByClient()

  return rows.map((c) => {
    const a = byClient.get(c.id)
    const operation = operations.get(c.id)
    return {
      accessStatus: operation ? accessStatus(operation) : UNCONFIRMED,
      id: c.id,
      name: c.name,
      filterTraffic: c.filterTraffic,
      expiresAt: c.expiresAt,
      deviceLimit: c.deviceLimit,
      frozenManual: c.frozenManual,
      createdAt: c.createdAt,
      status: clientStatus(c),
      tgLinked: !!c.tgChatId,
      tgUsername: c.tgUsername,
      preferredExit: c.preferredExit,
      deviceCount: a ? Number(a.cnt) : 0,
      rxTotal: a ? Number(a.rx) : 0,
      txTotal: a ? Number(a.tx) : 0,
    }
  })
}

/** Клиент из адреса с девайсами и последней операцией доступа: 400 на не число, 404 без клиента. */
export async function clientCard(event: H3Event) {
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const devRows = await db.select().from(devices).where(eq(devices.clientId, id)).orderBy(...devicesOldestFirst)
  const operation = await latestAccess(client.id)

  return {
    client,
    devices: devRows,
    operation,
    accessStatus: operation ? accessStatus(operation) : UNCONFIRMED,
    status: clientStatus(client),
    tgLinked: !!client.tgChatId,
  }
}
