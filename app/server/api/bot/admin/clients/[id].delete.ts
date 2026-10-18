import { deleteClient } from '../../../../utils/client-ops'
import { accessRequestKey } from '../../../../utils/access-operations'
import { requireBotAuth } from '../../../../utils/bot-api'
import { BOT_ACTOR } from '../../../../utils/events'

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const result = await deleteClient(id, BOT_ACTOR, accessRequestKey(event))
  setResponseStatus(event, 202)
  return result
})
