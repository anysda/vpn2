import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { eq } from 'drizzle-orm'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { expect, test } from '@playwright/test'
import type { AccessAdapter } from '../server/utils/access-adapter'
import type * as Operations from '../server/utils/access-operations'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'
import { accessStatus } from '../shared/access-operation'

const require = createRequire(import.meta.url)
const harness = require('./support/access-harness.cjs')
const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
let directory: string
let ops: typeof Operations
let schema: typeof Schema
let db: ReturnType<typeof Database.useDb>
let adapter: AccessAdapter
let fact: Map<string, number>

test.beforeEach(async () => {
  directory = mkdtempSync(path.join(tmpdir(), 'access-'))
  fact = new Map()
  adapter = {
    enabled: async () => ({ wg: true, openvpn: true, ikev2: false }),
    read: async (protocol, operation) => fact.get(`${protocol}:${operation.clientId}`) === operation.version,
    apply: async (protocol, operation) => { fact.set(`${protocol}:${operation.clientId}`, operation.version) },
  }
  const app = harness(root, path.join(directory, 'db.sqlite'), adapter)
  schema = app.load('app/server/database/schema.ts')
  db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
  await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
  ops = app.load('app/server/utils/access-operations.ts')
})
test.afterEach(() => { rmSync(directory, { recursive: true, force: true }) })

async function client() {
  const [c] = await db.insert(schema.clients).values({ name: 'operation test', password: 'fixture', tgChatId: 123 }).returning()
  return c!
}
async function due() { await db.update(schema.accessOperations).set({ nextRetry: 0 }) }

test('принятие, идемпотентность, отключённый протокол и однократное уведомление', async () => {
  const c = await client()
  const input = { clientId: c.id, actor: 'panel', key: 'freeze-1', patch: { frozenManual: true } }
  const first = await ops.enqueueAccess(input)
  expect(first.client!.frozenManual).toBe(true)
  expect(first.operation.status).toBe('pending')
  expect(accessStatus(first.operation)).toBe('Отключение выполняется')
  expect((await db.select().from(schema.botOutbox))).toHaveLength(0)
  expect((await ops.enqueueAccess(input)).operation.id).toBe(first.operation.id)
  await expect(ops.enqueueAccess({ ...input, patch: { frozenManual: false } })).rejects.toMatchObject({ statusCode: 409 })
  await ops.runAccessOperations(adapter)
  const completed = await ops.getAccess(first.operation.id)
  expect(accessStatus(completed)).toBe('Доступ отключён')
  expect(completed.protocols.ikev2.state).toBe('not_required')
  expect(completed.protocols.wg.confirmedVersion).toBe(completed.version)
  await due()
  await ops.runAccessOperations(adapter)
  expect((await db.select().from(schema.botOutbox))).toHaveLength(1)
  // Очистка доставленной очереди и старых событий не разрешает повторную рассылку.
  await db.delete(schema.botOutbox)
  await db.delete(schema.events)
  await due()
  await ops.runAccessOperations(adapter)
  expect((await db.select().from(schema.botOutbox))).toHaveLength(0)
  expect((await db.select().from(schema.events))).toHaveLength(0)
  console.log('accepted: Отключение выполняется; confirmed: Доступ отключён; IKEv2: Не требуется; notifications=1')
})

test('ошибка OpenVPN сохраняет цель удаления, повтор сначала читает факт', async () => {
  const c = await client()
  await db.insert(schema.devices).values({ clientId: c.id, name: 'phone', ovpnCert: 'retained certificate' })
  const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'bot', kind: 'delete' })
  const apply = adapter.apply
  adapter.apply = async (p, op, payload) => {
    if (p === 'openvpn') throw new Error('secret diagnostic must never escape')
    return apply(p, op, payload)
  }
  await ops.runAccessOperations(adapter)
  expect(accessStatus(await ops.getAccess(operation.id))).toBe('Отключение не завершено: OpenVPN')
  expect((await db.select().from(schema.clients))[0]!.deleting).toBe(true)
  expect((await db.select().from(schema.devices))[0]!.ovpnCert).toBe('retained certificate')
  expect((await db.select().from(schema.botOutbox))).toHaveLength(0)
  expect(JSON.stringify(await ops.getAccess(operation.id))).not.toContain('secret diagnostic')
  fact.set(`openvpn:${c.id}`, operation.version)
  adapter.apply = async () => { throw new Error('must read first') }
  await due()
  await ops.retryAccess(operation.id)
  await ops.runAccessOperations(adapter)
  expect((await ops.getAccess(operation.id)).status).toBe('confirmed')
  expect((await db.select().from(schema.clients))).toHaveLength(0)
  expect((await db.select().from(schema.botOutbox))).toHaveLength(1)
  console.log('OpenVPN failure: Отключение не завершено: OpenVPN; credentials retained; notifications=0; retry: confirmed, notifications=1')
})

test('панель freeze → бот unfreeze во время применения: старая версия не фиксируется', async () => {
  const c = await client()
  const freeze = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { frozenManual: true } })
  let unfreeze: Awaited<ReturnType<typeof ops.enqueueAccess>> | undefined
  adapter.apply = async (p, op) => {
    fact.set(`${p}:${op.clientId}`, op.version)
    if (!unfreeze) unfreeze = await ops.enqueueAccess({ clientId: c.id, actor: 'bot', patch: { frozenManual: false } })
  }
  await Promise.all([ops.runAccessOperations(adapter), ops.runAccessOperations(adapter)])
  expect((await ops.getAccess(freeze.operation.id)).status).toBe('superseded')
  expect((await db.select().from(schema.botOutbox))).toHaveLength(0)
  await ops.runAccessOperations(adapter)
  const latest = await ops.getAccess(unfreeze!.operation.id)
  expect(accessStatus(latest)).toBe('Доступ включён')
  expect((await db.select().from(schema.clients))[0]!.accessVersion).toBe(latest.version)
  expect(fact.get(`wg:${c.id}`)).toBe(latest.version)
  console.log('freeze v1: Заменена; unfreeze v2: Доступ включён; stale notifications=0')
})

test('SIGKILL после действия VPN: новый процесс читает факт и завершает ту же операцию', async () => {
  const c = await client()
  const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { frozenManual: true } })
  const actual = path.join(directory, 'actual.json')
  const script = `
    const fs = require('node:fs');
    const harness = require(process.env.HARNESS);
    const adapter = {
      enabled: async () => ({wg:true,openvpn:true,ikev2:false}),
      read: async (p,op) => fs.existsSync(process.env.ACTUAL) && JSON.parse(fs.readFileSync(process.env.ACTUAL))[p] === op.version,
      apply: async (p,op) => {
        const state = fs.existsSync(process.env.ACTUAL) ? JSON.parse(fs.readFileSync(process.env.ACTUAL)) : {};
        state[p] = op.version; fs.writeFileSync(process.env.ACTUAL,JSON.stringify(state));
        if (process.env.CRASH === '1') process.kill(process.pid,'SIGKILL');
      }
    };
    const app = harness(process.env.ROOT,process.env.DB,adapter);
    app.load('app/server/utils/access-operations.ts').runAccessOperations(adapter).then(()=>process.exit(0)).catch(()=>process.exit(1));
  `
  const env = { ...process.env, HARNESS: path.resolve('e2e/support/access-harness.cjs'), ROOT: root, DB: path.join(directory, 'db.sqlite'), ACTUAL: actual }
  const killed = spawnSync(process.execPath, ['-e', script], { env: { ...env, CRASH: '1' } })
  expect(killed.signal).toBe('SIGKILL')
  expect(existsSync(actual)).toBe(true)
  expect((await ops.getAccess(operation.id)).status).toBe('pending')
  await due()
  const recovered = spawnSync(process.execPath, ['-e', script], { env })
  expect(recovered.status, recovered.stderr.toString()).toBe(0)
  expect((await ops.getAccess(operation.id)).status).toBe('confirmed')
  expect(JSON.parse(readFileSync(actual, 'utf8'))).toEqual({ wg: operation.version, openvpn: operation.version })
  console.log('worker SIGKILL after WG action; new process: same ID, confirmedVersion=desiredVersion for WG/OpenVPN')
})

test('перевыпуск и удаление сохраняют старые реквизиты, повтор не создаёт новые ключи', async () => {
  const c = await client()
  const [device] = await db.insert(schema.devices).values({ clientId: c.id, name: 'phone', wgPublicKey: 'old', wgPrivateKey: 'private', ovpnCert: 'old cert' }).returning()
  const input = { clientId: c.id, deviceId: device!.id, actor: 'panel', kind: 'reissue' as const, key: 'rotate' }
  const issued = await ops.enqueueAccess(input)
  const [first] = await db.select().from(schema.devices)
  await ops.enqueueAccess(input)
  const [second] = await db.select().from(schema.devices)
  expect(second!.wgPublicKey).toBe(first!.wgPublicKey)
  const deletion = await ops.enqueueAccess({ ...input, kind: 'delete', key: 'delete' })
  expect((await ops.getAccess(issued.operation.id)).status).toBe('superseded')
  const [stored] = await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.id, deletion.operation.id))
  expect(JSON.parse(stored!.payload).retired.map((r: { wgPublicKey: string }) => r.wgPublicKey)).toContain('old')
})

test('желаемое состояние и операция откатываются вместе при ошибке записи', async () => {
  const c = await client()
  await db.run("CREATE TRIGGER reject_operation BEFORE INSERT ON access_operations BEGIN SELECT RAISE(ABORT, 'test'); END")
  await expect(ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { frozenManual: true } })).rejects.toThrow()
  const [saved] = await db.select().from(schema.clients)
  expect(saved!.frozenManual).toBe(false)
  expect(saved!.accessVersion).toBe(0)
  expect((await db.select().from(schema.accessOperations))).toHaveLength(0)
})

test('история завершённых удалений не вытесняет живые операции из очереди', async () => {
  for (let i = 0; i < 17; i++) {
    const c = await client()
    await ops.enqueueAccess({ clientId: c.id, actor: 'panel', kind: 'delete' })
    await ops.runAccessOperations(adapter)
  }
  await due()
  const c = await client()
  const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { frozenManual: true } })
  await ops.runAccessOperations(adapter)
  expect((await ops.getAccess(operation.id)).status).toBe('confirmed')
})

test('повторы ограничены по частоте; прежнее подтверждение не скрывает потерю связи', async () => {
  const c = await client()
  const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { frozenManual: true } })
  await ops.runAccessOperations(adapter)
  expect((await ops.getAccess(operation.id)).status).toBe('confirmed')
  let calls = 0
  adapter.read = async () => { calls++; throw new Error('offline') }
  adapter.apply = async () => {}
  await due()
  await ops.runAccessOperations(adapter)
  expect(accessStatus(await ops.getAccess(operation.id))).toBe('Состояние не подтверждено')
  const attempts = calls
  await ops.retryAccess(operation.id)
  await ops.runAccessOperations(adapter)
  expect(calls).toBe(attempts)
  expect((await db.select().from(schema.botOutbox))).toHaveLength(1)
})
