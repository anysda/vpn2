import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { routes } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { syncRoutesFile } from '../../utils/routes-sync'

const Body = z.object({ outbound: z.string().min(1).max(64) })

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const [row] = await db
    .update(routes)
    .set({ outbound: body.outbound })
    .where(eq(routes.id, id))
    .returning()

  if (!row) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  await syncRoutesFile().catch(err => useLogger().error({ err }, 'route sync failed'))
  return row
})
