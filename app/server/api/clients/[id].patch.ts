import { z } from 'zod'
import { requireAuth } from '../../utils/auth'
import { updateClient } from '../../utils/client-ops'
import { readBodyAs } from '../../utils/validate'

const Body = z.object({
  name: z.string().min(1).max(64).optional(),
  expiresAt: z.iso.datetime().nullable().optional(),
  // null — безлимит.
  deviceLimit: z.number().int().min(1).max(999).nullable().optional(),
  frozenManual: z.boolean().optional(),
  // тег узла экзита (hy2-<тег>-direct должен существовать) или null — авто
  preferredExit: z.string().regex(/^[a-z0-9-]{1,32}$/).nullable().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const body = await readBodyAs(event, Body)
  return updateClient(id, body)
})
