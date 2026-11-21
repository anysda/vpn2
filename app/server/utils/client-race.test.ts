import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { eq } from 'drizzle-orm'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { beforeAll, expect, it, vi } from 'vitest'
import { useDb } from '../database/client'
import * as schema from '../database/schema'

const url = `file:${join(mkdtempSync(join(tmpdir(), 'vpn2-race-')), 'panel.db')}`
vi.mock('./client-prefs', () => ({ syncClientPrefsFile: async () => {}, routerApplied: async () => true }))
vi.stubGlobal('useLogger', () => ({ info: () => {}, warn: () => {}, error: () => {} }))
vi.stubGlobal('useRuntimeConfig', () => ({ databaseUrl: url }))
vi.stubGlobal('createError', (fields: { statusCode: number, statusMessage?: string, message?: string }) =>
  Object.assign(new Error(fields.message ?? fields.statusMessage), fields))

beforeAll(async () => {
  await migrate(useDb(), { migrationsFolder: fileURLToPath(new URL('../database/migrations', import.meta.url)) })
})

const named = async (name: string) => (await useDb().select().from(schema.clients).where(eq(schema.clients.name, name))).length
const outcome = (results: PromiseSettledResult<unknown>[]) =>
  results.map(r => r.status === 'fulfilled' ? 'ok' : (r.reason as { statusCode?: number }).statusCode).sort()

it('два одновременных создания с одним именем: один клиент и один 409', async () => {
  const { ClientCreate, createClient } = await import('./client-ops')
  const make = () => createClient(ClientCreate.parse({ name: 'Bob' }), '@admin')
  expect(outcome(await Promise.allSettled([make(), make(), make()]))).toEqual([409, 409, 'ok'])
  expect(await named('Bob')).toBe(1)
})

it('переименование двух клиентов в одно имя одновременно: одно проходит', async () => {
  const { ClientCreate, createClient, updateClient } = await import('./client-ops')
  const [a, b] = [await createClient(ClientCreate.parse({ name: 'A' }), '@admin'), await createClient(ClientCreate.parse({ name: 'B' }), '@admin')]
  expect(outcome(await Promise.allSettled([updateClient(a.id, { name: 'Carl' }, '@admin'), updateClient(b.id, { name: 'Carl' }, '@admin')])))
    .toEqual([409, 'ok'])
  expect(await named('Carl')).toBe(1)
})

it('правило имён прежнее: регистр различается, пробелы по краям срезаются', async () => {
  const { ClientCreate, createClient } = await import('./client-ops')
  await createClient(ClientCreate.parse({ name: 'bob' }), '@admin')
  await expect(createClient(ClientCreate.parse({ name: ' bob ' }), '@admin')).rejects.toMatchObject({ statusCode: 409, statusMessage: 'client_exists' })
})

it('база 1.2.0 с двойниками: двойники остаются как есть, новое имя-двойник - 409', async () => {
  const { ClientCreate, createClient, updateClient } = await import('./client-ops')
  await useDb().insert(schema.clients).values([{ name: 'Dup', password: 'p' }, { name: 'Dup', password: 'p' }])
  await expect(createClient(ClientCreate.parse({ name: 'Dup' }), '@admin')).rejects.toMatchObject({ statusCode: 409 })
  const [first] = await useDb().select().from(schema.clients).where(eq(schema.clients.name, 'Dup'))
  await updateClient(first!.id, { deviceLimit: 5 }, '@admin')
  expect(await named('Dup')).toBe(2)
})
