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

export { schema }
