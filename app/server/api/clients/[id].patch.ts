import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { syncShadowsocksConfig } from '../../utils/shadowsocks'

const Body = z.object({
  name: z.string().min(1).max(64).optional(),
  enabled: z.boolean().optional(),
  expiresAt: z.iso.datetime().nullable().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const patch: Partial<typeof clients.$inferInsert> = { updatedAt: new Date() }
  if (body.name !== undefined) patch.name = body.name
  if (body.enabled !== undefined) patch.enabled = body.enabled
  if (body.expiresAt !== undefined) {
    patch.expiresAt = body.expiresAt ? new Date(body.expiresAt) : null
  }

  const [row] = await db.update(clients).set(patch).where(eq(clients.id, id)).returning()
  if (!row) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  if (body.enabled !== undefined) {
    await syncShadowsocksConfig().catch((err) => {
      useLogger().error({ err }, 'failed to sync ss config after enable toggle')
    })
  }

  return row
})
