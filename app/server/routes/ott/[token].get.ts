import { eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients, oneTimeLinks } from '../../database/schema'
import { buildSsUrl } from '../../utils/ss-url'

const GRACE_SECONDS = 10

export default defineEventHandler(async (event) => {
  const token = getRouterParam(event, 'token')
  if (!token) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_token' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.ssPublicHost) {
    throw createError({ statusCode: 500, statusMessage: 'ss_public_host_not_configured' })
  }

  const db = useDb()
  const [ott] = await db
    .select()
    .from(oneTimeLinks)
    .where(eq(oneTimeLinks.token, token))
    .limit(1)

  const now = new Date()
  if (!ott) {
    throw createError({ statusCode: 404, statusMessage: 'not_found_or_expired' })
  }
  if (ott.expiresAt < now) {
    throw createError({ statusCode: 410, statusMessage: 'expired' })
  }
  if (ott.usedAt && Date.now() - ott.usedAt.getTime() > GRACE_SECONDS * 1000) {
    throw createError({ statusCode: 410, statusMessage: 'already_used' })
  }

  const [client] = await db.select().from(clients).where(eq(clients.id, ott.clientId)).limit(1)
  if (!client) {
    throw createError({ statusCode: 404, statusMessage: 'client_not_found' })
  }

  if (!ott.usedAt) {
    await db
      .update(oneTimeLinks)
      .set({ usedAt: now })
      .where(eq(oneTimeLinks.id, ott.id))
  }

  const url = buildSsUrl({
    cipher: client.cipher,
    secret: client.ssSecret,
    host: cfg.ssPublicHost,
    port: Number(cfg.ssPort),
    name: client.name,
  })

  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return url
})
