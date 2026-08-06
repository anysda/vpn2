import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../database/client'
import { users } from '../../../database/schema'
import { requireAuth, verifyAdminPassword } from '../../../utils/auth'
import { verifyTotpToken } from '../../../utils/totp'

// Чтобы отключить 2FA, нужны оба фактора: текущий пароль И валидный TOTP-код.
// Без TOTP-проверки украденная сессия + leaked password могут снести 2FA — что
// и есть весь смысл «второго фактора». См. SECURITY-AUDIT-2026-06-01.md (M2).
const Body = z.object({
  currentPassword: z.string().min(1),
  totpCode: z.string().min(6).max(8),
})

export default defineEventHandler(async (event) => {
  const u = await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const [row] = await db.select().from(users).where(eq(users.id, u.id)).limit(1)
  if (!row || !(await verifyAdminPassword(row.passwordHash, body.currentPassword))) {
    throw createError({ statusCode: 401, statusMessage: 'invalid_current_password' })
  }
  if (!row.totpSecret || !verifyTotpToken(body.totpCode, row.totpSecret)) {
    throw createError({ statusCode: 401, statusMessage: 'invalid_totp' })
  }

  await db
    .update(users)
    .set({ totpSecret: null, updatedAt: new Date() })
    .where(eq(users.id, u.id))

  await setUserSession(event, {
    user: { id: u.id, username: u.username, totpEnabled: false, via: u.via },
  })

  return { ok: true }
})
