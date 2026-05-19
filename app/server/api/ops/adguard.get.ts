import { fetchAdguardStats } from '../../utils/agh-client'
import { requireAuth } from '../../utils/auth'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const snap = await fetchAdguardStats()
  if (!snap) {
    return { available: false }
  }
  return { available: true, ...snap }
})
