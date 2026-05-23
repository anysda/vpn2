import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../database/client'
import { users } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { verifyTotpToken } from '../../../utils/totp'

const Body = z.object({ code: z.string().min(6).max(8) })

// Принимает кандидат-секрет из server-side сессии (положил setup), проверяет
// первый код. На успех — пишет в users.totp_secret и чистит pending из сессии.
// Только после этого следующий логин начинает требовать TOTP.
export default defineEventHandler(async (event) => {
  const u = await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)

  const sess = await getUserSession(event)
  const pending = sess.secure?.pendingTotpSecret
  if (!pending) {
    throw createError({ statusCode: 400, statusMessage: 'totp_not_initiated' })
  }
  if (!verifyTotpToken(body.code, pending)) {
    throw createError({ statusCode: 401, statusMessage: 'invalid_totp' })
  }

  const db = useDb()
  await db
    .update(users)
    .set({ totpSecret: pending, updatedAt: new Date() })
    .where(eq(users.id, u.id))

  // Обновляем user.totpEnabled и сбрасываем pending. replaceUserSession
  // полностью заменяет сессию — иначе defu в setUserSession не позволяет
  // выкинуть pendingTotpSecret (merge не умеет deletion).
  await replaceUserSession(event, {
    user: { id: u.id, username: u.username, totpEnabled: true },
  })

  return { ok: true }
})
