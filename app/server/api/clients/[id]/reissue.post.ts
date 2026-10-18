import { requireAuth } from '../../../utils/auth'
import { actorOf } from '../../../utils/events'
import { accessRequestKey, enqueueAccess } from '../../../utils/access-operations'

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  if (!Number.isSafeInteger(clientId) || clientId < 1) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const { operation } = await enqueueAccess({ clientId, kind: 'reissue', actor: actorOf(user), key: accessRequestKey(event) })
  setResponseStatus(event, 202)
  return { operation }
})
