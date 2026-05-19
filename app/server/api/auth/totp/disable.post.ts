import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../database/client'
import { users } from '../../../database/schema'
import { requireAuth, verifyAdminPassword } from '../../../utils/auth'

const Body = z.object({ currentPassword: z.string().min(1) })

export default defineEventHandler(async (event) => {
  const u = await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const [row] = await db.select().from(users).where(eq(users.id, u.id)).limit(1)
  if (!row || !(await verifyAdminPassword(row.passwordHash, body.currentPassword))) {
    throw createError({ statusCode: 401, statusMessage: 'invalid_current_password' })
  }

  await db
    .update(users)
    .set({ totpSecret: null, updatedAt: new Date() })
    .where(eq(users.id, u.id))

  await setUserSession(event, {
    user: { id: u.id, username: u.username, totpEnabled: false },
  })

  return { ok: true }
})
