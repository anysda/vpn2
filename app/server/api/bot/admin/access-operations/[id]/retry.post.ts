import { requireBotAuth } from '../../../../../utils/bot-api'
import { retryAccess } from '../../../../../utils/access-operations'

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const operation = await retryAccess(getRouterParam(event, 'id') ?? '')
  setResponseStatus(event, 202)
  return { operation }
})
