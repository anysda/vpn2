import { createClient } from '@libsql/client'
import { dirname, join } from 'node:path'

/** Separate SQLite write lock: crash releases it without an expiring lease. */
export async function withAccessLock(action: () => Promise<void>): Promise<void> {
  const url = String(useRuntimeConfig().databaseUrl)
  if (!url.startsWith('file:')) throw new Error('access worker requires a local database')
  const lock = createClient({ url: `file:${join(dirname(url.slice(5)), 'access-worker.sqlite')}`, timeout: 0 })
  try {
    let tx
    try { tx = await lock.transaction('write') }
    catch (error) {
      if ((error as { code?: string }).code === 'SQLITE_BUSY') return
      throw error
    }
    try { await action() }
    finally { await tx.rollback() }
  }
  finally { lock.close() }
}
