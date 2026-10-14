import { and, eq, gte } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { clients, deviceTrafficHourly, devices } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { weekByMskDay } from '../../../utils/client-week'
import { mskDayStartSec } from '../../../utils/msk-day'

/** Трафик клиента за 7 московских суток: сумма по девайсам, ↓ и ↑ раздельно. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  const [client] = await db.select({ id: clients.id }).from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const now = Date.now()
  const rows = await db
    .select({ hour: deviceTrafficHourly.hour, rx: deviceTrafficHourly.rx, tx: deviceTrafficHourly.tx })
    .from(deviceTrafficHourly)
    .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
    .where(and(eq(devices.clientId, id), gte(deviceTrafficHourly.hour, mskDayStartSec(now, 6))))
  return weekByMskDay(rows, now)
})
