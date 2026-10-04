import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import * as schema from './schema'

let _db: ReturnType<typeof drizzle<typeof schema>> | null = null

export function useDb() {
  if (!_db) {
    const config = useRuntimeConfig()
    const client = createClient({ url: config.databaseUrl })
    _db = drizzle(client, { schema })
  }
  return _db
}

/**
 * Нарушение UNIQUE. drizzle заворачивает ошибку libsql в свою, поэтому
 * смотрим всю цепочку `cause`.
 */
export function isUniqueViolation(err: unknown): boolean {
  for (let e = err as { code?: unknown, message?: unknown, cause?: unknown } | undefined, i = 0; e && i < 5; e = e.cause as typeof e, i++) {
    if (e.code === 'SQLITE_CONSTRAINT_UNIQUE' || /UNIQUE constraint failed/.test(String(e.message ?? ''))) return true
  }
  return false
}

/**
 * Сколько раз пробовать занять адрес из пула. Каждый конфликт значит, что
 * параллельный запрос успел занять свой, так что запас ограничен только
 * числом одновременных созданий.
 */
export const IP_CLAIM_ATTEMPTS = 50
