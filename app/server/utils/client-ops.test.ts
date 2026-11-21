import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import { beforeAll, expect, it, vi } from 'vitest'
import * as schema from '../database/schema'

const state = vi.hoisted(() => ({ db: null as unknown, sync: vi.fn(async () => {}) }))
vi.mock('../database/client', () => ({ useDb: () => state.db }))
vi.mock('./client-prefs', () => ({ syncClientPrefsFile: state.sync, routerApplied: async (sync: () => Promise<unknown>) => sync().then(() => true, () => false) }))
vi.mock('./route-outbound', () => ({ assertKnownOutbound: async () => {} }))
vi.stubGlobal('useLogger', () => ({ info: () => {}, warn: () => {}, error: () => {} }))
vi.stubGlobal('useRuntimeConfig', () => ({}))
vi.stubGlobal('createError', (fields: { statusCode: number, statusMessage?: string, message?: string }) =>
  Object.assign(new Error(fields.message ?? fields.statusMessage), fields))

type Db = ReturnType<typeof drizzle<typeof schema>>
let db: Db

beforeAll(async () => {
  db = drizzle(createClient({ url: ':memory:' }), { schema })
  await migrate(db, { migrationsFolder: fileURLToPath(new URL('../database/migrations', import.meta.url)) })
  state.db = db
  await db.insert(schema.clients).values({ id: 1, name: 'Анна', password: 'p', deviceLimit: 3 })
})

const journal = async () => (await db.select().from(schema.events).orderBy(schema.events.id)).map(r => [r.type, r.title, r.detail])

it('смена экзита, имени и лимита - событие «было → стало» с автором', async () => {
  const { updateClient } = await import('./client-ops')
  await updateClient(1, { preferredExit: 'at' }, '@admin')
  await updateClient(1, { preferredExit: null }, '@admin')
  await updateClient(1, { name: 'Анна К', deviceLimit: null }, 'через бота')
  await updateClient(1, { name: 'Анна К', deviceLimit: null }, '@admin')
  expect(await journal()).toEqual([
    ['client_exit', 'Экзит изменён · Анна', 'авто → AT · @admin'],
    ['client_exit', 'Экзит изменён · Анна', 'AT → авто · @admin'],
    ['client_renamed', 'Имя изменено · Анна К', 'Анна → Анна К · через бота'],
    ['client_device_limit', 'Лимит устройств изменён · Анна К', '3 → без ограничений · через бота'],
  ])
})

it('имя, совпадающее с чужим после снятия невидимых символов, - 409', async () => {
  const { ClientCreate, createClient, updateClient } = await import('./client-ops')
  await expect(createClient(ClientCreate.parse({ name: 'Анна К​​' }), '@admin')).rejects.toMatchObject({ statusCode: 409, statusMessage: 'client_exists' })
  const boris = await createClient(ClientCreate.parse({ name: 'Борис' }), '@admin')
  await expect(updateClient(boris.id, ClientCreate.parse({ name: '⁠Анна К' }), '@admin')).rejects.toMatchObject({ statusCode: 409 })
})

it('сбой записи настроек роутеру - ошибка 502, событие записано', async () => {
  const { updateClient } = await import('./client-ops')
  state.sync.mockRejectedValueOnce(new Error('control down'))
  await expect(updateClient(1, { preferredExit: 'fi' }, '@admin')).rejects.toMatchObject({ statusCode: 502, statusMessage: 'prefs_not_applied' })
  expect((await journal()).at(-1)).toEqual(['client_exit', 'Экзит изменён · Анна К', 'авто → FI · @admin'])
  const [row] = await db.select().from(schema.clients)
  expect(row!.preferredExit).toBe('fi')
})
