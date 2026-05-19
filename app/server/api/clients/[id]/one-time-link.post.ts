import { eq } from 'drizzle-orm'
import { customAlphabet } from 'nanoid'
import { useDb } from '../../../database/client'
import { clients, oneTimeLinks } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'

const TTL_MINUTES = 5
const tokenAlphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
const generateToken = customAlphabet(tokenAlphabet, 15)

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  const token = generateToken()
  const expiresAt = new Date(Date.now() + TTL_MINUTES * 60 * 1000)

  await db.insert(oneTimeLinks).values({
    clientId: id,
    token,
    expiresAt,
  })

  return {
    token,
    url: `/ott/${token}`,
    expiresAt: expiresAt.toISOString(),
    ttlSeconds: TTL_MINUTES * 60,
  }
})
