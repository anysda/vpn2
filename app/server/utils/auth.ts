import { hash, verify } from '@node-rs/argon2'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { users } from '../database/schema'

export { buildTotpUri, generateTotpSecret, verifyTotpToken } from './totp'

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

export async function getAdmin() {
  const db = useDb()
  const rows = await db.select().from(users).limit(1)
  return rows[0] ?? null
}

export async function requireAuth(event: H3Event) {
  const session = await getUserSession(event)
  if (!session.user) {
    throw createError({ statusCode: 401, statusMessage: 'Unauthorized' })
  }
  return session.user
}
