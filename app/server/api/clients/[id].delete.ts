import { requireAuth } from '../../utils/auth'
import { actorOf } from '../../utils/events'
import { deleteClient } from '../../utils/client-ops'
import { accessRequestKey } from '../../utils/access-operations'

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const result = await deleteClient(id, actorOf(user), accessRequestKey(event))
  setResponseStatus(event, 202)
  return result
})
