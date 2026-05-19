import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { users } from '../../database/schema'
import { verifyAdminPassword } from '../../utils/auth'
import { verifyTotpToken } from '../../utils/totp'

const Body = z.object({
  username: z.string().min(1).max(64),
  password: z.string().min(1),
  totpCode: z.string().optional(),
})

export default defineEventHandler(async (event) => {
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()
  const [user] = await db
    .select()
    .from(users)
    .where(eq(users.username, body.username))
    .limit(1)

  if (!user || !(await verifyAdminPassword(user.passwordHash, body.password))) {
    throw createError({ statusCode: 401, statusMessage: 'invalid_credentials' })
  }

  if (user.totpSecret) {
    if (!body.totpCode) {
      return { needsTotp: true }
    }
    if (!verifyTotpToken(body.totpCode, user.totpSecret)) {
      throw createError({ statusCode: 401, statusMessage: 'invalid_totp' })
    }
  }

  await setUserSession(event, {
    user: {
      id: user.id,
      username: user.username,
      totpEnabled: !!user.totpSecret,
    },
  })

  return { ok: true }
})
