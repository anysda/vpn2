import { requireAuth } from '../../../../utils/auth'
import { actorOf } from '../../../../utils/events'
import { deleteDevice } from '../../../../utils/client-ops'
import { accessRequestKey } from '../../../../utils/access-operations'

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (![clientId, deviceId].every(id => Number.isSafeInteger(id) && id > 0)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const result = await deleteDevice(clientId, deviceId, actorOf(user), accessRequestKey(event))
  setResponseStatus(event, 202)
  return result
})
