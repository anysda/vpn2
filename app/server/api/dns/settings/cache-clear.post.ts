import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'

/** Очистить кэш DNS. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  await agh('cache_clear', { method: 'POST' })
  return { ok: true }
})
