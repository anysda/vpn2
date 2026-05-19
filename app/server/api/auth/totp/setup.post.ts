import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { users } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { buildTotpUri, generateTotpSecret } from '../../../utils/totp'

export default defineEventHandler(async (event) => {
  const u = await requireAuth(event)
  if (u.totpEnabled) {
    throw createError({ statusCode: 409, statusMessage: 'totp_already_enabled' })
  }

  const secret = generateTotpSecret()
  const db = useDb()

  // Store secret tentatively; user must confirm with a valid code via /confirm
  await db
    .update(users)
    .set({ totpSecret: secret, updatedAt: new Date() })
    .where(eq(users.id, u.id))

  return {
    secret,
    otpauthUrl: buildTotpUri(u.username, secret),
  }
})
