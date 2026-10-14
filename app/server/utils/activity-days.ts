import { eq, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { deviceTrafficHourly, devices } from '../database/schema'
import { PERIOD_ACTIVE_MIN_BYTES } from './client-activity'
import { MSK_OFFSET_SEC, mskDay } from './msk-day'

export interface ActivityDay {
  /** Номер суток МСК (дни от эпохи). */
  day: number
  /** Клиентов, активных за сутки (от PERIOD_ACTIVE_MIN_BYTES, как в списке клиентов). */
  online: number
  /** Трафик всех клиентов за сутки, байты. */
  bytes: number
}

/** Суммы клиентов по суткам → строка на сутки; сутки без записей не выдумываем. */
function foldDays(rows: Array<{ day: number, clientId: number, bytes: number }>): ActivityDay[] {
  const out = new Map<number, ActivityDay>()
  for (const r of rows) {
    const d = out.get(r.day) ?? { day: r.day, online: 0, bytes: 0 }
    d.bytes += r.bytes
    if (r.bytes >= PERIOD_ACTIVE_MIN_BYTES) d.online++
    out.set(r.day, d)
  }
  return [...out.values()].sort((a, b) => a.day - b.day)
}

/** Активность по суткам МСК за всю историю трафика (сборщик держит год, старые сутки свёрнуты). */
export async function activityDays(nowMs = Date.now()): Promise<{ today: number, days: ActivityDay[] }> {
  const day = sql<number>`cast((${deviceTrafficHourly.hour} + ${MSK_OFFSET_SEC}) / 86400 as integer)`
  const rows = await useDb()
    .select({
      day,
      clientId: devices.clientId,
      bytes: sql<number>`sum(${deviceTrafficHourly.rx} + ${deviceTrafficHourly.tx})`,
    })
    .from(deviceTrafficHourly)
    .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
    .groupBy(day, devices.clientId)
  return { today: mskDay(nowMs), days: foldDays(rows.map(r => ({ day: Number(r.day), clientId: r.clientId, bytes: Number(r.bytes) }))) }
}
