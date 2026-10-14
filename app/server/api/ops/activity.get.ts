import { requireAuth } from '../../utils/auth'
import { activityDays } from '../../utils/activity-days'

/** Сетка «Активность» на обзоре: клиенты в сети и трафик по суткам МСК. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return activityDays()
})
