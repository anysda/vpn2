import { eq, gte, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { deviceTrafficHourly, devices } from '../database/schema'
import { mskDayStartSec } from './msk-day'
import { recentBytesByDevice } from './traffic-collector'

/**
 * «В сети» — трафик за последние 5 минут от порога. Ниже порога — служебный
 * шум подключённого, но простаивающего устройства: WG PersistentKeepalive = 25
 * и рукопожатия дают ~0,5 КБ за 5 минут.
 */
export const ONLINE_MIN_BYTES = 20 * 1024

/** «Активен за сутки/неделю» — от 1 МБ за период (keepalive за сутки ~0,2 МБ). */
export const PERIOD_ACTIVE_MIN_BYTES = 1024 ** 2

export type ActivityPeriod = 'day' | 'week'

/** Начало периода, unix-секунды: сутки — с 00:00 МСК, неделя — 7 суток МСК. */
export function periodStartSec(period: ActivityPeriod, nowMs = Date.now()): number {
  return mskDayStartSec(nowMs, period === 'week' ? 6 : 0)
}

/** Трафик клиентов с `sinceSec` по почасовой истории: clientId → { rx, tx }. */
export async function periodTotalsByClient(sinceSec: number): Promise<Map<number, { rx: number, tx: number }>> {
  const rows = await useDb()
    .select({
      clientId: devices.clientId,
      rx: sql<number>`coalesce(sum(${deviceTrafficHourly.rx}), 0)`,
      tx: sql<number>`coalesce(sum(${deviceTrafficHourly.tx}), 0)`,
    })
    .from(deviceTrafficHourly)
    .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
    .where(gte(deviceTrafficHourly.hour, sinceSec))
    .groupBy(devices.clientId)
  return new Map(rows.map(r => [r.clientId, { rx: Number(r.rx), tx: Number(r.tx) }]))
}

/** Байты клиентов за последние 5 минут: clientId → bytes (только ненулевые). */
export async function recentBytesByClient(): Promise<Map<number, number>> {
  const byDevice = recentBytesByDevice()
  const out = new Map<number, number>()
  if (!byDevice.size) return out
  const rows = await useDb().select({ id: devices.id, clientId: devices.clientId }).from(devices)
  for (const r of rows) {
    const b = byDevice.get(r.id)
    if (b) out.set(r.clientId, (out.get(r.clientId) ?? 0) + b)
  }
  return out
}

/** Дата начала почасовой истории (unix-секунды) или null, если её ещё нет. */
export async function historyStartSec(): Promise<number | null> {
  const [row] = await useDb()
    .select({ first: sql<number | null>`min(${deviceTrafficHourly.hour})` })
    .from(deviceTrafficHourly)
  return row?.first == null ? null : Number(row.first)
}
