import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import { beforeAll, beforeEach, expect, it, vi } from 'vitest'
import * as schema from '../database/schema'

// Хост: vpn2-control поднят или нет, деплой идёт или нет, файлы роутера.
const host = vi.hoisted(() => ({ up: true, deploy: 'idle', files: {} as Record<string, unknown>, writes: 0, routeWrites: 0, db: null as unknown }))
vi.mock('../database/client', () => ({ useDb: () => host.db }))
vi.mock('./auth', () => ({ requireAuth: async () => ({ id: 1, username: 'admin' }) }))
vi.mock('./api', async importOriginal => ({ ...await importOriginal<object>(), readBodyAs: async (e: { body: unknown }) => e.body }))
vi.mock('./route-outbound', () => ({ assertKnownOutbound: async () => {} }))
vi.mock('./control', () => ({
  ControlUnavailable: class extends Error {},
  control: async (method: string) => {
    if (!host.up) throw new Error('control down')
    if (method === 'manager.status.read') return { deploy: host.deploy }
    throw new Error(method)
  },
  controlApply: async (_: string, params: { resource: string, config: unknown }) => {
    if (!host.up) throw new Error('control down')
    host.files[params.resource] = params.config
    host.writes++
    if (params.resource === 'routes') host.routeWrites++
  },
}))
vi.mock('./access-operations', () => ({ enqueueExpiredAccess: async () => {}, runAccessOperations: async () => {} }))
vi.mock('./expiry', () => ({ reportControlExpiry: async () => {}, syncControlExpiry: async () => {} }))
vi.mock('./traffic', () => ({ collectTraffic: async () => {} }))
vi.mock('./nodes', async importOriginal => ({ ...await importOriginal<object>(), exitNodes: () => [] }))
vi.stubGlobal('createError', (fields: { statusCode: number, statusMessage?: string, message?: string }) =>
  Object.assign(new Error(fields.message ?? fields.statusMessage), fields))
vi.stubGlobal('useLogger', () => ({ info: () => {}, warn: () => {}, error: () => {} }))
vi.stubGlobal('useRuntimeConfig', () => ({}))
vi.stubGlobal('getRouterParam', (e: { params: Record<string, string> }, name: string) => e.params[name])

let db: ReturnType<typeof drizzle<typeof schema>>
beforeAll(async () => {
  db = drizzle(createClient({ url: ':memory:' }), { schema })
  await migrate(db, { migrationsFolder: fileURLToPath(new URL('../database/migrations', import.meta.url)) })
  host.db = db
})
beforeEach(async () => {
  Object.assign(host, { up: true, deploy: 'idle', files: {}, writes: 0, routeWrites: 0 })
  await db.delete(schema.routes)
  await db.delete(schema.collectorState)
})

type Route = 'POST /routes' | 'PATCH /routes/:id' | 'DELETE /routes/:id'
const call = async (route: Route, body?: unknown, id?: number) =>
  (await import('../resources/routes')).default[route]({ body, params: { id: String(id) } } as never) as unknown as Promise<{ id: number }>
const add = (value: string) => call('POST /routes', { value, outbound: 'direct-ru' })
const fileValues = () => (host.files.routes as { value: string }[] | undefined)?.map(r => r.value)
const notApplied = { statusCode: 502, statusMessage: 'routes_not_applied', message: 'Маршруты сохранены, но роутер их ещё не получил: повтор раз в минуту' }

/** Один минутный тик cron настоящего плагина. */
async function cronTick() {
  vi.useFakeTimers()
  try {
    vi.stubGlobal('defineNitroPlugin', (p: unknown) => p)
    vi.resetModules()
    const plugin = (await import('../plugins/cron')).default as unknown as (app: unknown) => void
    const closers: Array<() => Promise<void>> = []
    plugin({ hooks: { hook: (_: string, fn: () => Promise<void>) => closers.push(fn) } })
    await vi.advanceTimersByTimeAsync(5_000)
    for (const close of closers) await close()
  }
  finally { vi.useRealTimers() }
}

it('роутер принял: 200, правило в файле', async () => {
  const row = await add('a.example')
  expect(row.id).toBeGreaterThan(0)
  expect(fileValues()).toEqual(['a.example'])
})

it.each([
  ['vpn2-control остановлен', () => { host.up = false }],
  ['идёт деплой', () => { host.deploy = 'busy' }],
])('%s: добавление, смена выхода и удаление - 502, правка в базе, файл не тронут', async (_, down) => {
  const kept = await add('keep.example')
  host.writes = 0
  down()
  await expect(add('b.example')).rejects.toMatchObject(notApplied)
  await expect(call('PATCH /routes/:id', { outbound: 'hy2-at-direct' }, kept.id)).rejects.toMatchObject(notApplied)
  await expect(call('DELETE /routes/:id', undefined, kept.id)).rejects.toMatchObject(notApplied)
  expect((await db.select().from(schema.routes)).map(r => r.value)).toEqual(['b.example'])
  expect(host.writes).toBe(0)
  expect(fileValues()).toEqual(['keep.example'])
})

it('расхождение доезжает минутным тиком, когда служба поднялась и деплой кончился', async () => {
  host.up = false
  await expect(add('c.example')).rejects.toMatchObject(notApplied)
  host.up = true
  host.deploy = 'busy'
  await cronTick()
  expect(fileValues()).toBeUndefined()
  host.deploy = 'idle'
  await cronTick()
  expect(fileValues()).toEqual(['c.example'])
})

it('панель перезапущена, пока служба лежала: первый тик после старта пишет правила из базы', async () => {
  await db.insert(schema.routes).values([{ type: 'domain', value: 'd.example', outbound: 'direct-ru' }, { type: 'domain', value: 'e.example', outbound: 'direct-ru' }])
  await cronTick()
  expect(fileValues()).toEqual(['d.example', 'e.example'])
})

it('два одинаковых правила одновременно: одно создано, второе - 409', async () => {
  const results = await Promise.allSettled([add('f.example'), add('f.example')])
  expect(results.map(r => r.status === 'fulfilled' ? 'ok' : (r.reason as { statusCode: number }).statusCode).sort()).toEqual([409, 'ok'])
})

it('несколько правок во время деплоя: после него в файле последнее состояние базы', async () => {
  host.deploy = 'busy'
  const g = await add('g.example').catch(() => null)
  expect(g).toBeNull()
  await add('h.example').catch(() => {})
  const [first] = await db.select().from(schema.routes)
  await call('DELETE /routes/:id', undefined, first!.id).catch(() => {})
  host.deploy = 'idle'
  await cronTick()
  expect(fileValues()).toEqual(['h.example'])
})

it('чистый хост без правил: минутный тик маршруты роутеру не шлёт', async () => {
  await cronTick()
  await cronTick()
  expect(host.routeWrites).toBe(0)
})

it('правка применена: следующие тики её не переписывают', async () => {
  await add('i.example')
  await cronTick()
  await cronTick()
  expect(host.routeWrites).toBe(1)
})

it('все правила удалены, пока служба лежала: тик пишет пустой список', async () => {
  const row = await add('j.example')
  host.up = false
  await expect(call('DELETE /routes/:id', undefined, row.id)).rejects.toMatchObject(notApplied)
  host.up = true
  await cronTick()
  expect(fileValues()).toEqual([])
})
