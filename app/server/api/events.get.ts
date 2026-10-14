import { z } from 'zod'
import { requireAuth } from '../utils/auth'
import { recentEvents } from '../utils/events'

const Query = z.object({ limit: z.coerce.number().int().min(1).max(100).default(4) })

/** Последние события для ленты на Обзоре, новые первыми. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { limit } = await getValidatedQuery(event, Query.parse)
  return recentEvents(limit)
})
