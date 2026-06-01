import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { users } from '../../database/schema'
import { verifyAdminPassword } from '../../utils/auth'
import { rateLimitClear, rateLimitGuard, rateLimitRecordFailure } from '../../utils/rate-limit'
import { verifyTotpToken } from '../../utils/totp'

const Body = z.object({
  username: z.string().min(1).max(64),
  password: z.string().min(1),
  totpCode: z.string().optional(),
})

// 10 неудачных попыток за 5 мин с одного IP → блок на 5 мин. См. SECURITY-AUDIT
// 2026-06-01.md (M1). Argon2-cost (~200ms) сам по себе не остановит онлайн-перебор.
const RL = { scope: 'login', maxAttempts: 10, windowMs: 5 * 60_000, lockoutMs: 5 * 60_000 }

export default defineEventHandler(async (event) => {
  rateLimitGuard(event, RL)
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()
  const [user] = await db
    .select()
    .from(users)
    .where(eq(users.username, body.username))
    .limit(1)

  if (!user || !(await verifyAdminPassword(user.passwordHash, body.password))) {
    rateLimitRecordFailure(event, RL)
    throw createError({ statusCode: 401, statusMessage: 'invalid_credentials' })
  }

  if (user.totpSecret) {
    if (!body.totpCode) {
      return { needsTotp: true }
    }
    if (!verifyTotpToken(body.totpCode, user.totpSecret)) {
      rateLimitRecordFailure(event, RL)
      // Один и тот же statusMessage для password/TOTP — убирает password-oracle
      // из M4. Бэкенду все равно, какой фактор сломан; клиент UX тоже опирается
      // на needsTotp/ok, а не на текст ошибки.
      throw createError({ statusCode: 401, statusMessage: 'invalid_credentials' })
    }
  }

  rateLimitClear(event, RL)
  await setUserSession(event, {
    user: {
      id: user.id,
      username: user.username,
      totpEnabled: !!user.totpSecret,
    },
  })

  return { ok: true }
})
