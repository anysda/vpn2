import { requireAuth } from '../../../utils/auth'
import { retryAccess } from '../../../utils/access-operations'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const operation = await retryAccess(getRouterParam(event, 'id') ?? '')
  setResponseStatus(event, 202)
  return { operation }
})
