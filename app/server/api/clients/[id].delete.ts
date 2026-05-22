import { requireAuth } from '../../utils/auth'
import { deleteClient } from '../../utils/client-ops'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  await deleteClient(id)
  return { ok: true }
})
