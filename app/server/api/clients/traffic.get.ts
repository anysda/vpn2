import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'

/**
 * Накопительный трафик по клиентам — суммарно за всё время по всем
 * протоколам (Shadowsocks + WireGuard + OpenVPN).
 *
 * Источник — колонки clients.rx_total / tx_total, которые наполняет фоновый
 * сборщик (server/utils/traffic-collector.ts, вызывается из cron-плагина).
 * Поэтому трафик виден у всех клиентов, а не только подключённых по Outline.
 *
 * Returns { [clientId]: { rxBytes, txBytes } }.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const db = useDb()
  const rows = await db
    .select({ id: clients.id, rx: clients.rxTotal, tx: clients.txTotal })
    .from(clients)

  const result: Record<number, { rxBytes: number, txBytes: number }> = {}
  for (const r of rows) {
    result[r.id] = { rxBytes: r.rx, txBytes: r.tx }
  }
  return result
})
