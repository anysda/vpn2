import { requireAuth } from '../../utils/auth'
import { getAccess } from '../../utils/access-operations'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return getAccess(getRouterParam(event, 'id') ?? '')
})
