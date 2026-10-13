import { createClient, type Client, type InArgs, type InStatement, type Transaction, type TransactionMode } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import * as schema from './schema'

let _db: ReturnType<typeof drizzle<typeof schema>> | null = null

/** Один владелец соединения до конца транзакции, включая её async-шаги. */
function panelClient(url: string): Client {
  const client = createClient({ url, concurrency: 1, timeout: 5000 })
  // WAL сохраняется в файле; timeout — на каждом соединении, в том числе
  // после reconnect. Ожидание SQLite нужно только для ДРУГОГО процесса:
  // блокировать event loop в ожидании собственной транзакции нельзя.
  let ready = client.execute('PRAGMA journal_mode=WAL')
  void ready.catch(() => {})
  let tail = Promise.resolve()
  async function acquire() {
    const previous = tail
    let release!: () => void
    tail = new Promise<void>(resolve => { release = resolve })
    await previous
    try { await ready }
    catch (error) { release(); throw error }
    return release
  }
  async function run<T>(action: () => Promise<T>): Promise<T> {
    const release = await acquire()
    try { return await action() }
    finally { release() }
  }
  return {
    execute: (stmt: InStatement, args?: InArgs) => run(() => typeof stmt === 'string' ? client.execute(stmt, args) : client.execute(stmt)),
    batch: (stmts, mode) => run(() => client.batch(stmts, mode)),
    migrate: stmts => run(() => client.migrate(stmts)),
    executeMultiple: statements => run(() => client.executeMultiple(statements)),
    sync: () => run(() => client.sync()),
    async transaction(mode: TransactionMode = 'write'): Promise<Transaction> {
      const release = await acquire()
      try {
        const tx = await client.transaction(mode)
        const close = () => {
          try { tx.close() }
          finally { release() }
        }
        return {
          execute: stmt => tx.execute(stmt),
          batch: statements => tx.batch(statements),
          executeMultiple: statements => tx.executeMultiple(statements),
          async commit() { try { await tx.commit() } finally { close() } },
          async rollback() { try { await tx.rollback() } finally { close() } },
          close,
          get closed() { return tx.closed },
        }
      }
      catch (error) { release(); throw error }
    },
    close: () => client.close(),
    reconnect() {
      client.reconnect()
      ready = client.execute('PRAGMA journal_mode=WAL')
      void ready.catch(() => {})
    },
    get closed() { return client.closed },
    get protocol() { return client.protocol },
  }
}

export function useDb() {
  if (!_db) {
    const config = useRuntimeConfig()
    const client = panelClient(config.databaseUrl)
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
