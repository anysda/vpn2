import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { eq, sql } from 'drizzle-orm'
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
let app: { load: (relative: string) => Record<string, unknown>, overrides: Map<string, unknown> }

test.beforeEach(async () => {
  directory = mkdtempSync(path.join(tmpdir(), 'access-'))
  fact = new Map()
  adapter = {
    enabled: async () => ({ wg: true, openvpn: true, ikev2: false }),
    read: async (protocol, operation) => fact.get(`${protocol}:${operation.clientId}`) === operation.version,
    apply: async (protocol, operation) => { fact.set(`${protocol}:${operation.clientId}`, operation.version) },
  }
  app = harness(root, path.join(directory, 'db.sqlite'), adapter)
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
  const history = await ops.getAccess(operation.id)
  for (const state of Object.values(history.protocols)) state.lastAttempt = 0
  await db.update(schema.accessOperations).set({ protocols: history.protocols }).where(eq(schema.accessOperations.id, operation.id))
  expect((await ops.retryAccess(operation.id)).status).toBe('confirmed')
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
  expect((await ops.currentAccessByClient()).get(c.id)?.id).toBe(latest.id)
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

test('уведомление о новом сроке сохраняется и доставляется только после подтверждения', async () => {
  const c = await client()
  await ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { expiresAt: new Date('2031-05-20T12:00:00Z') } })
  expect((await db.select().from(schema.botOutbox))).toHaveLength(0)
  await ops.runAccessOperations(adapter)
  const [notice] = await db.select().from(schema.botOutbox)
  expect(JSON.parse(notice!.payload).text).toBe('Доступ включён\nСрок действия вашего ключа: до 20.05.2031.')
})

test('истечение срока посреди применения не подтверждает включение', async () => {
  const c = await client()
  const clock = Date
  const deadline = clock.now() + 60_000
  const { operation } = await ops.enqueueAccess({ clientId: c.id, actor: 'panel', patch: { expiresAt: new Date(deadline) } })
  const read = adapter.read
  adapter.read = async (...args) => {
    globalThis.Date = new Proxy(clock, {
      construct: (target, args) => Reflect.construct(target, args.length ? args : [deadline + 1000]),
      get: (target, key) => key === 'now' ? () => deadline + 1000 : Reflect.get(target, key),
    })
    return read(...args)
  }
  try {
    await ops.runAccessOperations(adapter)
    expect((await ops.getAccess(operation.id)).status).toBe('pending')
    expect(await db.select().from(schema.botOutbox)).toHaveLength(0)
    await due()
    await ops.runAccessOperations(adapter)
    expect((await ops.getAccess(operation.id)).status).toBe('superseded')
    await ops.runAccessOperations(adapter)
    expect(accessStatus((await ops.latestAccess(c.id))!)).toBe('Доступ отключён')
    const notices = await db.select().from(schema.botOutbox)
    expect(notices).toHaveLength(1)
    expect(JSON.parse(notices[0]!.payload).text).toBe('Доступ отключён')
    console.log('expiry during enable: no enabled confirmation or notification; newer freeze confirmed')
  }
  finally { globalThis.Date = clock }
})

test('первичная сверка старой базы идёт через очередь без изменения версии и рассылки', async () => {
  const c = await client()
  await ops.enqueueExpiredAccess()
  const operation = await ops.latestAccess(c.id)
  expect(operation?.version).toBe(0)
  expect(operation?.status).toBe('pending')
  await ops.runAccessOperations(adapter)
  expect((await ops.getAccess(operation!.id)).status).toBe('confirmed')
  await ops.enqueueExpiredAccess()
  expect((await db.select().from(schema.accessOperations))).toHaveLength(1)
  expect((await db.select().from(schema.botOutbox))).toHaveLength(0)
  expect((await db.select().from(schema.events))).toHaveLength(0)
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
  adapter.apply = async () => { throw new Error('VPN service unavailable') }
  await due()
  await ops.runAccessOperations(adapter)
  expect(accessStatus(await ops.getAccess(operation.id))).toBe('Состояние не подтверждено')
  const attempts = calls
  const retry = await ops.retryAccess(operation.id)
  expect(retry.nextRetry - Math.max(...Object.values(retry.protocols).map(p => p.lastAttempt ?? 0))).toBe(5000)
  expect(retry.protocols.openvpn.nextRetry).toBe(retry.nextRetry)
  await ops.runAccessOperations(adapter)
  expect(calls).toBe(attempts)
  expect((await db.select().from(schema.botOutbox))).toHaveLength(1)
})

async function threeDevices(c: { id: number }) {
  return db.insert(schema.devices).values([0, 1, 2].map(i => ({
    clientId: c.id, name: `dev-${i}`, wgPublicKey: `confirmed-${i}`, wgPrivateKey: `private-${i}`, wgIp: `10.66.66.${i + 2}`, ovpnCert: `cert-${i}`,
  }))).returning()
}

test('VPN2-510: 200 перевыпусков без подтверждения - retired по одной записи на устройство, история не растёт', async () => {
  const c = await client()
  const created = await threeDevices(c)
  for (let i = 0; i < 200; i++) {
    await ops.enqueueAccess({ clientId: c.id, actor: 'panel', kind: 'reissue', key: `reissue-${i}`, deviceId: i % 4 === 3 ? undefined : created[i % 4]!.id })
  }
  const rows = await db.select().from(schema.accessOperations)
  expect(rows).toHaveLength(200)
  const [last] = rows.filter(row => row.status === 'pending')
  const payload = JSON.parse(last!.payload)
  // Отзывать надо то, что VPN-служба подтверждала до цепочки: исходные ключи и сертификаты.
  expect(payload.retired).toEqual(created.map(d => ({ id: d.id, wgPublicKey: d.wgPublicKey, ovpnCert: d.ovpnCert })))
  expect(payload.rotateOvpn.map((r: { id: number }) => r.id).sort()).toEqual(created.map(d => d.id).sort())
  for (const row of rows.filter(row => row.status === 'superseded')) {
    expect(JSON.parse(row.payload)).toMatchObject({ retired: [], rotateOvpn: [], name: 'operation test' })
  }
  const [{ largest }] = await db.all<{ largest: number }>(sql`SELECT max(length(payload)) AS largest FROM access_operations`)
  expect(largest).toBeLessThan(1024)
})

test('VPN2-510: реквизиты протокола, уже подтверждённого предыдущей операцией, дальше не тянутся', async () => {
  const c = await client()
  const [device] = await threeDevices(c)
  const first = await ops.enqueueAccess({ clientId: c.id, deviceId: device!.id, actor: 'panel', kind: 'reissue', key: 'first' })
  const [stored] = await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.id, first.operation.id))
  const protocols = structuredClone(stored!.protocols)
  protocols.wg = { ...protocols.wg, state: 'confirmed', confirmedVersion: stored!.version }
  await db.update(schema.accessOperations).set({ protocols }).where(eq(schema.accessOperations.id, stored!.id))
  const [current] = await db.select().from(schema.devices).where(eq(schema.devices.id, device!.id))
  const second = await ops.enqueueAccess({ clientId: c.id, deviceId: device!.id, actor: 'panel', kind: 'reissue', key: 'second' })
  const [row] = await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.id, second.operation.id))
  // WG старый ключ уже снят: отзывается ключ первой операции; OpenVPN не подтверждён - исходный сертификат остаётся.
  expect(JSON.parse(row!.payload).retired).toEqual([{ id: device!.id, wgPublicKey: current!.wgPublicKey, ovpnCert: 'cert-0' }])
})

test('VPN2-510: раздутая прежней версией база чистится при старте и ужимается', async () => {
  const c = await client()
  const created = await threeDevices(c)
  const entry = (i: number) => ({ id: created[i % 3]!.id, wgPublicKey: `key-${i}`, ovpnCert: 'x'.repeat(1500) })
  const bloated = (n: number) => JSON.stringify({ enabled: true, retired: Array.from({ length: n }, (_, i) => entry(i)), rotateOvpn: [], name: 'operation test', chatId: null })
  const protocols = { wg: { state: 'pending' }, openvpn: { state: 'pending' }, ikev2: { state: 'not_required' } } as never
  for (let v = 1; v <= 30; v++) {
    await db.insert(schema.accessOperations).values({
      id: `op-${v}`, clientId: c.id, kind: 'reissue', version: v, idempotencyKey: `k-${v}`, requestHash: 'h', protocols,
      status: v === 30 ? 'pending' : 'superseded', payload: bloated(v * 25), actor: 'panel', nextRetry: 0, createdAt: v,
    })
  }
  await db.update(schema.clients).set({ accessVersion: 30 }).where(eq(schema.clients.id, c.id))
  const size = async () => (await db.all<{ bytes: number }>(sql`SELECT page_count * page_size AS bytes FROM pragma_page_count(), pragma_page_size()`))[0]!.bytes
  const before = await size()
  expect(before).toBeGreaterThan(16 * 1024 * 1024)
  expect(await ops.compactAccessOperations()).toEqual({ rows: 30, vacuumed: true })
  const [live] = await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.id, 'op-30'))
  expect(JSON.parse(live!.payload).retired.map((r: { wgPublicKey: string }) => r.wgPublicKey)).toEqual(['key-0', 'key-1', 'key-2'])
  for (const row of await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.status, 'superseded'))) {
    expect(JSON.parse(row.payload)).toMatchObject({ enabled: true, retired: [], rotateOvpn: [] })
  }
  expect(await size()).toBeLessThan(before / 10)
  expect(await ops.compactAccessOperations()).toEqual({ rows: 0, vacuumed: false })
})

test('VPN2-510: место, не освобождённое прошлым запуском, освобождается при следующем', async () => {
  const c = await client()
  await threeDevices(c)
  const size = async () => (await db.all<{ bytes: number }>(sql`SELECT page_count * page_size AS bytes FROM pragma_page_count(), pragma_page_size()`))[0]!.bytes
  // Строки уже чистые (их почистил прошлый запуск), а VACUUM тогда упал: свободные страницы остались в файле.
  await db.run(sql`CREATE TABLE filler (data BLOB)`)
  for (let i = 0; i < 20; i++) await db.run(sql`INSERT INTO filler VALUES (randomblob(1048576))`)
  await db.run(sql`DROP TABLE filler`)
  const before = await size()
  expect(before).toBeGreaterThan(16 * 1024 * 1024)
  expect(await ops.compactAccessOperations()).toEqual({ rows: 0, vacuumed: true })
  expect(await size()).toBeLessThan(before / 10)
})

test('VPN2-510: промежуточный ключ WG на адресе устройства не подтверждается, хотя retired его не помнит', async () => {
  const c = await client()
  const [device] = await threeDevices(c)
  const { operation } = await ops.enqueueAccess({ clientId: c.id, deviceId: device!.id, actor: 'panel', kind: 'reissue', key: 'reissue' })
  const [current] = await db.select().from(schema.devices).where(eq(schema.devices.id, device!.id))
  let peers: Record<string, string[]> = {}
  app.overrides.delete('app/server/utils/access-adapter.ts')
  app.overrides.set('app/server/utils/control.ts', {
    control: async (method: string) => method === 'state.read' ? { peers } : {},
    ControlUnavailable: class extends Error {},
  })
  const real = app.load('app/server/utils/access-adapter.ts').accessAdapter as AccessAdapter
  const payload = JSON.parse((await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.id, operation.id)))[0]!.payload)
  const own = (key: string, i: number) => [key, [`10.66.66.${i + 2}/32`, `fd66:66::${i + 2}/128`]]
  peers = Object.fromEntries([own(current!.wgPublicKey!, 0), own('confirmed-1', 1), own('confirmed-2', 2)])
  expect(await real.read('wg', operation, payload)).toBe(true)
  // Ключ из середины цепочки перевыпусков, применённый чужой синхронизацией.
  peers = Object.fromEntries([own(current!.wgPublicKey!, 0), own('confirmed-1', 1), own('confirmed-2', 2), own('intermediate', 0)])
  expect(await real.read('wg', operation, payload)).toBe(false)
})
