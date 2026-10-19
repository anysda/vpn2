import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { expect, test } from '@playwright/test'
import type { AccessAdapter } from '../server/utils/access-adapter'
import type * as Operations from '../server/utils/access-operations'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'
import { accessProtocols, accessStatus, protocolNames } from '../shared/access-operation'
import type { AccessProtocol } from '../shared/access-operation'

// Каждый протокол по отдельности: сбой одного не подтверждает операцию,
// автоповтор без кнопки доводит её, когда служба снова отвечает.
const require = createRequire(import.meta.url)
const harness = require('./support/access-harness.cjs')
const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
let directory: string
let ops: typeof Operations
let schema: typeof Schema
let db: ReturnType<typeof Database.useDb>
let adapter: AccessAdapter
let fact: Map<string, number>
let broken: AccessProtocol | null
let surviving: AccessProtocol | null

test.beforeEach(async () => {
  directory = mkdtempSync(path.join(tmpdir(), 'access-protocols-'))
  fact = new Map()
  broken = null
  surviving = null
  adapter = {
    enabled: async () => ({ wg: true, openvpn: true, ikev2: true }),
    read: async (protocol, operation) => fact.get(`${protocol}:${operation.clientId}`) === operation.version,
    apply: async (protocol, operation) => {
      // Служба отказала: syncconf, CRL или --load-creds не прошли.
      if (protocol === broken) throw new Error('unavailable')
      // Служба ответила, а сессия осталась: факт не совпадает с целью.
      if (protocol === surviving) return
      fact.set(`${protocol}:${operation.clientId}`, operation.version)
    },
  }
  const app = harness(root, path.join(directory, 'db.sqlite'), adapter)
  schema = app.load('app/server/database/schema.ts')
  db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
  await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
  ops = app.load('app/server/utils/access-operations.ts')
})
test.afterEach(() => { rmSync(directory, { recursive: true, force: true }) })

async function client() {
  const [c] = await db.insert(schema.clients).values({ name: 'protocol test', password: 'fixture', tgChatId: 123 }).returning()
  await db.insert(schema.devices).values({ clientId: c!.id, name: 'phone', ovpnCert: 'retained certificate' })
  return c!
}
async function due() { await db.update(schema.accessOperations).set({ nextRetry: 0 }) }

for (const protocol of accessProtocols) {
  const name = protocolNames[protocol]

  test(`${name}: отказ применения не завершает заморозку, автоповтор доводит`, async () => {
    const c = await client()
    const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', key: `freeze-${protocol}`, patch: { frozenManual: true } })
    broken = protocol
    await ops.runAccessOperations(adapter)
    const failed = await ops.getAccess(operation.id)
    expect(accessStatus(failed)).toBe(`Отключение не завершено: ${name}`)
    for (const other of accessProtocols.filter(p => p !== protocol)) expect(failed.protocols[other].state).toBe('confirmed')
    expect(failed.nextRetry).toBeGreaterThan(Date.now())
    broken = null
    await due()
    await ops.runAccessOperations(adapter)
    expect(accessStatus(await ops.getAccess(operation.id))).toBe('Доступ отключён')
  })

  test(`${name}: живая сессия после применения не даёт подтверждения`, async () => {
    const c = await client()
    const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', key: `session-${protocol}`, patch: { frozenManual: true } })
    surviving = protocol
    await ops.runAccessOperations(adapter)
    expect(accessStatus(await ops.getAccess(operation.id))).toBe(`Отключение не завершено: ${name}`)
    await due()
    await ops.runAccessOperations(adapter)
    expect(accessStatus(await ops.getAccess(operation.id))).toBe(`Отключение не завершено: ${name}`)
    surviving = null
    await due()
    await ops.runAccessOperations(adapter)
    expect(accessStatus(await ops.getAccess(operation.id))).toBe('Доступ отключён')
  })

  test(`${name}: удаление при частичном сбое держит реквизиты, повтор доводит отзыв`, async () => {
    const c = await client()
    const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'bot', kind: 'delete' })
    broken = protocol
    await ops.runAccessOperations(adapter)
    expect(accessStatus(await ops.getAccess(operation.id))).toBe(`Отключение не завершено: ${name}`)
    expect((await db.select().from(schema.clients))[0]!.deleting).toBe(true)
    expect((await db.select().from(schema.devices))[0]!.ovpnCert).toBe('retained certificate')
    broken = null
    await due()
    await ops.runAccessOperations(adapter)
    expect((await ops.getAccess(operation.id)).status).toBe('confirmed')
    expect(await db.select().from(schema.clients)).toHaveLength(0)
    expect(await db.select().from(schema.devices)).toHaveLength(0)
  })
}
