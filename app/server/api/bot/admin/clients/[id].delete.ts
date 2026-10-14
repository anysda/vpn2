import { deleteClient } from '../../../../utils/client-ops'
import { requireBotAuth } from '../../../../utils/bot-api'
import { BOT_ACTOR } from '../../../../utils/events'

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  await deleteClient(id, BOT_ACTOR)
  return { ok: true }
})
