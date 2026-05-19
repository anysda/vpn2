import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { clients } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { buildSsUrl } from '../../../utils/ss-url'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.ssPublicHost) {
    throw createError({ statusCode: 500, statusMessage: 'ss_public_host_not_configured' })
  }

  const db = useDb()
  const [row] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!row) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  const url = buildSsUrl({
    cipher: row.cipher,
    secret: row.ssSecret,
    host: cfg.ssPublicHost,
    port: Number(cfg.ssPort),
    name: row.name,
  })

  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return url
})
