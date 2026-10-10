import { sql } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { ONLINE_MIN_BYTES, periodRange, periodTotalsByClient, recentBytesByClient } from '../../utils/client-activity'
import { currentRatesByClient } from '../../utils/client-rates'

/**
 * Трафик по клиентам — сумма по их девайсам, по всем протоколам
 * (WireGuard + OpenVPN + IKEv2). Наполняет фоновый сборщик
 * (server/utils/traffic-collector.ts).
 *
 * Returns { [clientId]: {
 *   rxBytes, txBytes   — за всё время (devices.rx_total / tx_total),
 *   dayRx, dayTx       — с 00:00 МСК (почасовая история),
 *   recentBytes        — за последние 5 минут,
 *   online             — recentBytes ≥ ONLINE_MIN_BYTES,
 *   rxBps, txBps       — текущая скорость (байт/с, сглажено ~6 с),
 * } }.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const db = useDb()
  const [rows, day, recent, rates] = await Promise.all([
    db
      .select({
        clientId: devices.clientId,
        rx: sql<number>`coalesce(sum(${devices.rxTotal}), 0)`,
        tx: sql<number>`coalesce(sum(${devices.txTotal}), 0)`,
      })
      .from(devices)
      .groupBy(devices.clientId),
    periodTotalsByClient(periodRange('day').since),
    recentBytesByClient(),
    currentRatesByClient(),
  ])

  const result: Record<number, {
    rxBytes: number
    txBytes: number
    dayRx: number
    dayTx: number
    recentBytes: number
    online: boolean
    rxBps: number
    txBps: number
  }> = {}
  for (const r of rows) {
    const d = day.get(r.clientId)
    const recentBytes = recent.get(r.clientId) ?? 0
    result[r.clientId] = {
      rxBytes: Number(r.rx),
      txBytes: Number(r.tx),
      dayRx: d?.rx ?? 0,
      dayTx: d?.tx ?? 0,
      recentBytes,
      online: recentBytes >= ONLINE_MIN_BYTES,
      rxBps: Math.round(rates.get(r.clientId)?.rxBps ?? 0),
      txBps: Math.round(rates.get(r.clientId)?.txBps ?? 0),
    }
  }
  return result
})
