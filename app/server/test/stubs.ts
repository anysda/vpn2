// Подставки автоимпортов Nitro и модулей базы/авторизации для vitest серверных утилит.
// Модули подменяются в тесте так:
//   vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
// а installStubs() в beforeEach ставит глобалы и сбрасывает состояние.
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

type Chain = { [method: string]: (...args: unknown[]) => Chain } & PromiseLike<unknown>

function chain(op: string): Chain {
  calls.push(`db:${op}`)
  let result: Promise<unknown> | undefined
  const proxy: Chain = new Proxy({} as Chain, {
    get(_target, prop) {
      if (prop === 'then') {
        result ??= Promise.resolve(dbResults.shift() ?? [])
        return result.then.bind(result)
      }
      return (...args: unknown[]) => {
        dbOps.push({ op: String(prop), args })
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
  clientByChat: async () => {
    calls.push('db:clientByChat')
    const [client] = dbResults.shift() ?? []
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
