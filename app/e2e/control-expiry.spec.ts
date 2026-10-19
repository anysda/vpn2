import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { eq } from 'drizzle-orm'
import { expect, test } from '@playwright/test'
import type * as Operations from '../server/utils/access-operations'
import type * as Adapter from '../server/utils/access-adapter'
import type * as Expiry from '../server/utils/expiry'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'

// Служба снимает доступ по сроку сама, пока панели нет. Панель остаётся
// источником истины: срок доходит до службы, продление отменяет снятие,
// после простоя операция сходится без повторного снятия.
const require = createRequire(import.meta.url)
const harness = require('./support/access-harness.cjs')
const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
const KEY = 'expiry-test-public-key'
let directory: string
let ops: typeof Operations
let adapter: Adapter.AccessAdapter
let expiry: typeof Expiry
let schema: typeof Schema
let db: ReturnType<typeof Database.useDb>
let peers: Map<string, string | null>
let removed: Array<{ protocol: string, id: string, expires_at: string, at: string }>
let applies: number

function day(offset: number) {
  return new Date(Math.floor(Date.now() / 1000 + offset * 86400) * 1000)
}

test.beforeEach(async () => {
  directory = mkdtempSync(path.join(tmpdir(), 'control-expiry-'))
  peers = new Map()
  removed = []
  applies = 0
  const app = harness(root, path.join(directory, 'db.sqlite'), null, { wgEnabled: true })
  app.overrides.delete('app/server/utils/access-adapter.ts')
  const control = async (method: string, params: Record<string, unknown>) => {
    if (method === 'state.read') {
      return { peers: Object.fromEntries([...peers.keys()].map(key => [key, []])),
        expires: Object.fromEntries([...peers].filter(([, at]) => at)) }
    }
    if (method === 'wg.apply') {
      applies++
      peers = new Map((params.peers as Array<{ key: string, expires_at?: string | null }>)
        .map(peer => [peer.key, peer.expires_at ?? null]))
      return { ok: true }
    }
    if (method === 'expiry.run') return { removed, latest: [], failed: [] }
    throw new Error(`unexpected ${method}`)
  }
  app.overrides.set('app/server/utils/control.ts', { control, controlApply: control, ControlUnavailable: Error })
  schema = app.load('app/server/database/schema.ts')
  db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
  await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
  ops = app.load('app/server/utils/access-operations.ts')
  adapter = (app.load('app/server/utils/access-adapter.ts') as typeof Adapter).accessAdapter
  expiry = app.load('app/server/utils/expiry.ts')
})
test.afterEach(() => { rmSync(directory, { recursive: true, force: true }) })

async function client(expiresAt: Date) {
  const [c] = await db.insert(schema.clients).values({ name: 'Срок', password: 'fixture', tgChatId: 123, expiresAt }).returning()
  await db.insert(schema.devices).values({ clientId: c!.id, name: 'телефон', wgPublicKey: KEY, wgPrivateKey: 'private',
    wgPresharedKey: 'preshared', wgIp: '10.66.66.2' })
  await ops.enqueueAccess({ clientId: c!.id, actor: 'panel', key: `create-${c!.id}` })
  await ops.runAccessOperations(adapter)
  return c!
}

test('срок доходит до службы, продление до истечения сохраняет доступ', async () => {
  const soon = day(1)
  const c = await client(soon)
  expect(peers.get(KEY)).toBe(soon.toISOString())
  const later = day(30)
  const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', key: 'extend', patch: { expiresAt: later } })
  // Служба держит старый срок: операция не подтверждена, пока продление не дошло.
  expect(await adapter.read('wg', operation, { enabled: true, retired: [], rotateOvpn: [], name: c.name, chatId: null })).toBe(false)
  await ops.runAccessOperations(adapter)
  expect(peers.get(KEY)).toBe(later.toISOString())
  expect((await ops.getAccess(operation.id)).protocols.wg.state).toBe('confirmed')
})

test('истёк при остановленной панели: после запуска операция сходится без повторного снятия, журнал один раз', async () => {
  const soon = day(1)
  const c = await client(soon)
  const before = applies
  // Панель стоит, служба сняла пира по своему таймеру.
  const past = day(-1)
  await db.update(schema.clients).set({ expiresAt: past }).where(eq(schema.clients.id, c.id))
  peers.delete(KEY)
  removed.push({ protocol: 'wg', id: KEY, expires_at: past.toISOString(), at: past.toISOString() })
  // Панель запустилась: такт cron.
  await ops.enqueueExpiredAccess()
  await ops.runAccessOperations(adapter)
  await expiry.reportControlExpiry()
  await expiry.reportControlExpiry()
  expect(applies).toBe(before)
  expect(peers.has(KEY)).toBe(false)
  const operations = await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.clientId, c.id))
  const last = await ops.getAccess(operations.at(-1)!.id)
  expect(last.protocols.wg.state).toBe('confirmed')
  const events = await db.select().from(schema.events).where(eq(schema.events.type, 'access_expired_offline'))
  expect(events.map(e => [e.title, e.object, e.detail]))
    .toEqual([['Срок истёк, доступ снят сервером · Срок · телефон', `client:${c.id}`, 'WireGuard']])
})

test('срок, которого служба не знает (обновление панели, правка базы), доходит до неё тактом cron', async () => {
  const c = await client(day(1))
  const soon = new Date(Math.floor(Date.now() / 1000 + 180) * 1000)
  await db.update(schema.clients).set({ expiresAt: soon }).where(eq(schema.clients.id, c.id))
  await expiry.syncControlExpiry()
  expect(peers.get(KEY)).toBe(soon.toISOString())
  const before = applies
  await expiry.syncControlExpiry()
  expect(applies).toBe(before)
})
