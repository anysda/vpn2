import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ what: z.enum(['log', 'stats']) })

/** Сбросить журнал запросов или статистику. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { what } = await readBodyAs(event, Body)
  await agh(what === 'log' ? 'querylog_clear' : 'stats_reset', { method: 'POST' })
  return { ok: true }
})
