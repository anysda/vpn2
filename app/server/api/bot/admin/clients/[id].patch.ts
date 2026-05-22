import { z } from 'zod'
import { updateClient } from '../../../../utils/client-ops'
import { requireBotAuth } from '../../../../utils/bot-api'

const Body = z.object({
  name: z.string().min(1).max(64).optional(),
  expiresAt: z.iso.datetime().nullable().optional(),
  deviceLimit: z.number().int().min(1).max(999).nullable().optional(),
  frozenManual: z.boolean().optional(),
})

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const body = await readValidatedBody(event, Body.parse)
  return updateClient(id, body)
})
