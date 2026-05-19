import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../database/client'
import { users } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { verifyTotpToken } from '../../../utils/totp'

const Body = z.object({ code: z.string().min(6).max(8) })

export default defineEventHandler(async (event) => {
  const u = await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const [row] = await db.select().from(users).where(eq(users.id, u.id)).limit(1)
  if (!row?.totpSecret) {
    throw createError({ statusCode: 400, statusMessage: 'totp_not_initiated' })
  }
  if (!verifyTotpToken(body.code, row.totpSecret)) {
    throw createError({ statusCode: 401, statusMessage: 'invalid_totp' })
  }

  // Refresh session with totpEnabled=true
  await setUserSession(event, {
    user: { id: u.id, username: u.username, totpEnabled: true },
  })

  return { ok: true }
})
