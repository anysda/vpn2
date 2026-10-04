import { hash, verify } from '@node-rs/argon2'
import { eq, sql } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { users } from '../database/schema'

export async function hashAdminPassword(plain: string): Promise<string> {
  return hash(plain, {
    memoryCost: 65536,
    timeCost: 3,
    parallelism: 4,
  })
}

export async function verifyAdminPassword(hashed: string, plain: string): Promise<boolean> {
  try {
    return await verify(hashed, plain)
  }
  catch {
    return false
  }
}

/**
 * Сессия в cookie без состояния на сервере, поэтому отзыв — через поколение:
 * в cookie лежит users.session_version на момент входа, здесь оно сверяется с
 * БД. Учётку удалили или поколение сменилось — сессия мертва.
 */
export async function requireAuth(event: H3Event) {
  const session = await getUserSession(event)
  if (!session.user) {
    throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
  }
  const [row] = await useDb()
    .select({ sv: users.sessionVersion })
    .from(users)
    .where(eq(users.id, session.user.id))
    .limit(1)
  if (!row || row.sv !== (session.user.sv ?? 0)) {
    await clearUserSession(event)
    throw createError({ statusCode: 401, statusMessage: 'session_revoked' })
  }
  return session.user
}

/** Отзывает все сессии учётки. Возвращает новое поколение — для текущей сессии. */
export async function bumpSessionVersion(userId: number): Promise<number> {
  const [row] = await useDb()
    .update(users)
    .set({ sessionVersion: sql`${users.sessionVersion} + 1`, updatedAt: new Date() })
    .where(eq(users.id, userId))
    .returning({ sv: users.sessionVersion })
  if (!row) throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
  return row.sv
}
