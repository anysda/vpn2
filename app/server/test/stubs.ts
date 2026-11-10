// Подставки автоимпортов Nitro и модулей базы/авторизации для vitest серверных утилит.
// Модули подменяются в тесте так:
//   vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
// а installStubs() в beforeEach ставит глобалы и сбрасывает состояние.
import { Column, Param, SQL, getTableColumns, getTableName, is } from 'drizzle-orm'
import { vi } from 'vitest'

export interface StubError extends Error {
  statusCode: number
  statusMessage?: string
  cause?: unknown
}

/** Последовательность обращений: auth, preflight (пишет тест), db:<операция>, fetch. */
export const calls: string[] = []
/** Ответы базы по очереди: каждый await цепочки запроса берёт следующий, пусто - []. */
export const dbResults: unknown[][] = []
/** Вызовы методов цепочек запроса (where, set, values...) с аргументами. */
export const dbOps: { op: string, args: unknown[] }[] = []
/**
 * Строки таблиц по имени таблицы ('clients', 'devices'). Если для таблицы строки
 * заданы, select().from(таблица).where(...) отбирает их по условию запроса, а не
 * берёт очередь dbResults: снятая проверка владельца отдаёт чужую строку.
 */
export const dbRows: Record<string, Record<string, unknown>[]> = {}
export const runtimeConfig: Record<string, unknown> = {}
export const fetchMock = vi.fn<(url: string, opts: Record<string, unknown>) => Promise<unknown>>()
/** Отказ авторизации: тест подставляет throw. */
export const auth = { admin: vi.fn<() => void>(), bot: vi.fn<() => void>() }

export function stubError(input: { statusCode: number, statusMessage?: string, message?: string, cause?: unknown }): StubError {
  const err = new Error(input.message ?? input.statusMessage) as StubError
  err.statusCode = input.statusCode
  err.statusMessage = input.statusMessage
  if ('cause' in input) err.cause = input.cause
  return err
}

/** Условие where как пары «колонка = значение»; порядок и скобки and() не важны. */
export function conditionPairs(cond: unknown, out: { column: string, value: unknown }[] = []) {
  const tokens: unknown[] = []
  const walk = (node: unknown) => {
    if (is(node, Column)) tokens.push({ column: node })
    else if (is(node, Param)) tokens.push({ value: node.value })
    else if (is(node, SQL)) node.queryChunks.forEach(walk)
    else if (Array.isArray(node)) node.forEach(walk)
  }
  walk(cond)
  tokens.forEach((t, i) => {
    const next = tokens[i + 1] as { value?: unknown } | undefined
    if ('column' in (t as object) && next && 'value' in next) out.push({ column: (t as { column: Column }).column.name, value: next.value })
  })
  return out
}

function selectRows(table: unknown, cond: unknown): Record<string, unknown>[] | undefined {
  const rows = is(table, SQL) || !table ? undefined : dbRows[getTableName(table as never)]
  if (!rows) return undefined
  const keys = Object.entries(getTableColumns(table as never) as Record<string, Column>).map(([key, col]) => [col.name, key] as const)
  const pairs = conditionPairs(cond).map(({ column, value }) => [keys.find(([name]) => name === column)?.[1] ?? column, value] as const)
  return rows.filter(row => pairs.every(([key, value]) => row[key] === value))
}

type Chain = { [method: string]: (...args: unknown[]) => Chain } & PromiseLike<unknown>

function chain(op: string): Chain {
  calls.push(`db:${op}`)
  let result: Promise<unknown> | undefined
  let table: unknown
  let cond: unknown
  const proxy: Chain = new Proxy({} as Chain, {
    get(_target, prop) {
      if (prop === 'then') {
        result ??= Promise.resolve(op === 'select' && selectRows(table, cond) || dbResults.shift() || [])
        return result.then.bind(result)
      }
      return (...args: unknown[]) => {
        dbOps.push({ op: String(prop), args })
        if (prop === 'from') table = args[0]
        if (prop === 'where') cond = args[0]
        return proxy
      }
    },
  })
  return proxy
}

function query(op: string) {
  return (...args: unknown[]) => {
    dbOps.push({ op, args })
    return chain(op)
  }
}

interface FakeDb {
  select: (...args: unknown[]) => Chain
  insert: (...args: unknown[]) => Chain
  update: (...args: unknown[]) => Chain
  delete: (...args: unknown[]) => Chain
  transaction: <T>(cb: (tx: FakeDb) => Promise<T>) => Promise<T>
}

export const fakeDb: FakeDb = {
  select: query('select'),
  insert: query('insert'),
  update: query('update'),
  delete: query('delete'),
  transaction: async cb => cb(fakeDb),
}

export const databaseModule = { useDb: () => fakeDb }
export const authModule = {
  requireAuth: async () => {
    calls.push('auth')
    auth.admin()
    return { id: 1, username: 'admin' }
  },
}
/** clientByChat берёт клиента из очереди dbResults, как select. */
export const botApiModule = {
  requireBotAuth: () => {
    calls.push('auth')
    auth.bot()
  },
  clientByChat: async (chatId: number) => {
    calls.push('db:clientByChat')
    const [client] = dbRows.clients ? dbRows.clients.filter(c => c.tgChatId === chatId) : dbResults.shift() ?? []
    if (!client) throw stubError({ statusCode: 404, statusMessage: 'not_linked' })
    return client
  },
  requireChatLinked: async () => { calls.push('db:chatLinked') },
}

export function fakeEvent(params: Record<string, string>, query: Record<string, string> = {}) {
  return { context: { params }, query } as never
}

/** Глобалы автоимпорта и чистое состояние; звать в beforeEach. */
export function installStubs(config: Record<string, unknown> = {}) {
  calls.length = 0
  dbResults.length = 0
  dbOps.length = 0
  for (const key of Object.keys(dbRows)) Reflect.deleteProperty(dbRows, key)
  for (const key of Object.keys(runtimeConfig)) Reflect.deleteProperty(runtimeConfig, key)
  Object.assign(runtimeConfig, config)
  fetchMock.mockReset()
  fetchMock.mockImplementation(async () => { calls.push('fetch') })
  auth.admin.mockReset()
  auth.bot.mockReset()
  vi.stubGlobal('createError', stubError)
  vi.stubGlobal('useRuntimeConfig', () => runtimeConfig)
  vi.stubGlobal('$fetch', fetchMock)
  vi.stubGlobal('getRouterParam', (e: { context: { params: Record<string, string> } }, name: string) => e.context.params[name])
  vi.stubGlobal('getQuery', (e: { query: Record<string, string> }) => e.query)
  vi.stubGlobal('setHeader', () => {})
  vi.stubGlobal('useLogger', () => ({ warn: () => {}, info: () => {}, error: () => {} }))
}

/** Ошибка, которой отклонился промис; не отклонился - тест падает. */
export async function rejection(promise: Promise<unknown>): Promise<StubError> {
  try {
    await promise
  }
  catch (err) {
    return err as StubError
  }
  throw new Error('ожидалась ошибка')
}
