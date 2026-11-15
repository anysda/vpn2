import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import { expect, it, vi } from 'vitest'
import * as schema from '../database/schema'

const state = vi.hoisted(() => ({ db: null as unknown }))
vi.mock('../database/client', () => ({ useDb: () => state.db }))
vi.stubGlobal('useLogger', () => ({ info: () => {}, warn: () => {}, error: () => {} }))

it('перебор пароля: попытки с адреса за 10 минут - одна строка со счётчиком', async () => {
  const db = drizzle(createClient({ url: ':memory:' }), { schema })
  await migrate(db, { migrationsFolder: fileURLToPath(new URL('../database/migrations', import.meta.url)) })
  state.db = db
  const { journalFailedLogin } = await import('./bot')
  const rows = async () => (await db.select().from(schema.events).orderBy(schema.events.id)).map(r => [r.title, r.detail])
  const start = Date.UTC(2026, 10, 15, 12)
  vi.useFakeTimers({ now: start })

  await db.insert(schema.events).values({ at: start - 1000, type: 'client_created', level: 'info', title: 'Клиент создан · Анна' })
  await journalFailedLogin('198.51.100.7', 'логин «admin»')
  expect(await rows()).toEqual([['Клиент создан · Анна', ''], ['Неудачный вход в панель', 'логин «admin» · 198.51.100.7']])

  // Ещё 9 за 2 с, три из них параллельно.
  for (let i = 0; i < 6; i++) {
    vi.setSystemTime(start + 200 * (i + 1))
    await journalFailedLogin('198.51.100.7', 'логин «admin»')
  }
  vi.setSystemTime(start + 2000)
  await Promise.all([1, 2, 3].map(() => journalFailedLogin('198.51.100.7', 'логин «admin»')))
  await journalFailedLogin('203.0.113.9', 'неверный код 2FA')
  expect(await rows()).toEqual([
    ['Клиент создан · Анна', ''],
    ['10 неудачных входов за 2 с', 'логин «admin» · 198.51.100.7'],
    ['Неудачный вход в панель', 'неверный код 2FA · 203.0.113.9'],
  ])
  // Строка серии поднимается ко времени последней попытки.
  expect((await db.select().from(schema.events).orderBy(schema.events.id))[1]!.at).toBe(start + 2000)

  // Через 10 минут от первой попытки - новая строка.
  vi.setSystemTime(start + 10 * 60_000 + 1)
  await journalFailedLogin('198.51.100.7', 'логин «root»')
  expect((await rows()).at(-1)).toEqual(['Неудачный вход в панель', 'логин «root» · 198.51.100.7'])
  vi.useRealTimers()
})
