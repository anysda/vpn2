import { sql } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'

/**
 * Накопительный трафик по клиентам — сумма по их девайсам, за всё время,
 * по всем протоколам (WireGuard + OpenVPN). Источник — devices.rx_total /
 * tx_total, наполняет фоновый сборщик (server/utils/traffic-collector.ts).
 *
 * Returns { [clientId]: { rxBytes, txBytes } }.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const db = useDb()
  const rows = await db
    .select({
      clientId: devices.clientId,
      rx: sql<number>`coalesce(sum(${devices.rxTotal}), 0)`,
      tx: sql<number>`coalesce(sum(${devices.txTotal}), 0)`,
    })
    .from(devices)
    .groupBy(devices.clientId)

  const result: Record<number, { rxBytes: number, txBytes: number }> = {}
  for (const r of rows) {
    result[r.clientId] = { rxBytes: Number(r.rx), txBytes: Number(r.tx) }
  }
  return result
})
