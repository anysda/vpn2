import { requireAuth } from '../../utils/auth'
import { exitTraffic } from '../../utils/exit-traffic'

/** Трафик за сутки или неделю по экзитам для обзора: ?period=day|week. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const period = getQuery(event).period === 'week' ? 'week' : 'day'
  return exitTraffic(period)
})
