import { eq, gte, sql } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients, deviceTrafficHourly, devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { ONLINE_MIN_BYTES, recentBytesByClient } from '../../utils/client-activity'

const HOURS = 24

/**
 * Клиенты онлайн и офлайн для обзора.
 *
 * total  — все клиенты;
 * online — сейчас в сети (трафик за 5 минут от ONLINE_MIN_BYTES, как в списке клиентов);
 * hourly — 24 последних часа, старый первым: сколько клиентов набрали за час
 *          от ONLINE_MIN_BYTES. Час без записей — 0, ряд не рвётся.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const hourNow = Math.floor(Date.now() / 3_600_000) * 3600
  const since = hourNow - (HOURS - 1) * 3600
  const db = useDb()
  const [[count], recent, rows] = await Promise.all([
    db.select({ n: sql<number>`count(*)` }).from(clients),
    recentBytesByClient(),
    db
      .select({
        hour: deviceTrafficHourly.hour,
        clientId: devices.clientId,
        bytes: sql<number>`sum(${deviceTrafficHourly.rx} + ${deviceTrafficHourly.tx})`,
      })
      .from(deviceTrafficHourly)
      .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
      .where(gte(deviceTrafficHourly.hour, since))
      .groupBy(deviceTrafficHourly.hour, devices.clientId),
  ])

  const hourly = Array.from({ length: HOURS }, () => 0)
  for (const r of rows) {
    const i = Math.floor((Number(r.hour) - since) / 3600)
    if (i >= 0 && i < HOURS && Number(r.bytes) >= ONLINE_MIN_BYTES) hourly[i]!++
  }
  let online = 0
  for (const b of recent.values()) if (b >= ONLINE_MIN_BYTES) online++

  return { total: Number(count?.n ?? 0), online, hourly }
})
