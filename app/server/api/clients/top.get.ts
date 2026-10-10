import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import {
  ONLINE_MIN_BYTES,
  PERIOD_ACTIVE_MIN_BYTES,
  ACTIVITY_PERIODS,
  historyStartSec,
  periodRange,
  periodTotalsByClient,
  recentBytesByClient,
} from '../../utils/client-activity'
import type { ActivityPeriod } from '../../utils/client-activity'

/**
 * ТОП клиентов по трафику за период (`?period=day|yesterday|week`) и счётчики
 * активности. Сутки — с 00:00 МСК, вчера — прошлые сутки МСК, неделя — 7 суток
 * МСК. В `clients` — все
 * с ненулевым трафиком за период, по убыванию. `counts` от периода не
 * зависят: блок цифр в UI не прыгает при переключении.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const q = getQuery(event)
  if (q.period !== undefined && !ACTIVITY_PERIODS.includes(q.period as ActivityPeriod)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_period' })
  }
  const period = (q.period ?? 'day') as ActivityPeriod

  const range = periodRange(period)
  const [rows, day, week, totals, recent, historyStart] = await Promise.all([
    useDb().select({ id: clients.id, name: clients.name }).from(clients),
    periodTotalsByClient(periodRange('day').since),
    periodTotalsByClient(periodRange('week').since),
    periodTotalsByClient(range.since, range.until),
    recentBytesByClient(),
    historyStartSec(),
  ])

  const top = rows
    .map((c) => {
      const t = totals.get(c.id)
      const rx = t?.rx ?? 0
      const tx = t?.tx ?? 0
      return { id: c.id, name: c.name, rx, tx, total: rx + tx }
    })
    .filter(c => c.total > 0)
    .sort((a, b) => b.total - a.total)

  const activeIn = (m: Map<number, { rx: number, tx: number }>) =>
    [...m.values()].filter(t => t.rx + t.tx >= PERIOD_ACTIVE_MIN_BYTES).length

  return {
    period,
    since: range.since,
    until: range.until,
    historyStart,
    clients: top,
    counts: {
      total: rows.length,
      onlineNow: [...recent.values()].filter(b => b >= ONLINE_MIN_BYTES).length,
      activeDay: activeIn(day),
      activeWeek: activeIn(week),
    },
  }
})
