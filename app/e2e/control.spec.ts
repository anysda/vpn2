import { spawn } from 'node:child_process'
import { once, EventEmitter } from 'node:events'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path, { join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import { serverModule, FakeClock } from './support/server-module'
import { createRequire } from 'node:module'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { eq } from 'drizzle-orm'
import type * as Operations from '../server/utils/access-operations'
import type * as Adapter from '../server/utils/access-adapter'
import type * as Expiry from '../server/utils/expiry'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'

test.describe('control-backlog', () => {
  const infra = resolve(process.env.E2E_SERVER_ROOT || '.', '../infra/control')
  const source = resolve(process.env.E2E_SERVER_ROOT || '.', '../infra/vpn2/src')

  // Настоящий serve() из пакета vpn2 (vpn2.control.server): соединения по одному, очередь
  // listen как Backlog в vpn2-control.socket. Ответ занимает 20 мс, как чтение
  // настроек на нагруженной машине.
  const SERVICE = `
import os, socket, sys, time
sys.path.insert(0, sys.argv[1])
from vpn2.control.server import serve

class Backend:
    def execute(self, method, params):
        time.sleep(0.02)
        return {'config': {'disabled': []}}

with socket.socket(socket.AF_UNIX) as listener:
    listener.bind(sys.argv[2])
    listener.listen(int(sys.argv[3]))
    print('ready', flush=True)
    serve(listener, Backend(), {os.getuid()}, {})
`

  test('всплеск запросов к сервису управления сверх очереди listen не теряется', async () => {
    const backlog = Number(/^Backlog=(\d+)$/m.exec(readFileSync(join(infra, 'vpn2-control.socket'), 'utf8'))![1])
    const directory = mkdtempSync(join(tmpdir(), 'vpn2-control-'))
    const path = join(directory, 'control.sock')
    const service = spawn('python3', ['-c', SERVICE, source, path, String(backlog)], { stdio: ['ignore', 'pipe', 'inherit'] })
    try {
      await once(service.stdout, 'data')
      const { control } = serverModule<typeof import('../server/utils/control')>('server/utils/control.ts', {
        'drizzle-orm': {},
        '../database/client': {},
        '../database/schema': {},
      }, { process: { env: { VPN2_CONTROL_SOCKET: path } }, setTimeout, clearTimeout, Buffer })
      // Тридцать одновременных PATCH экзитов — тридцать чтений сразу; берём втрое больше очереди.
      const calls = await Promise.allSettled(Array.from({ length: backlog * 3 }, () =>
        control('settings.read', { resource: 'disabled-exits' })))
      const failed = calls.filter(call => call.status === 'rejected').length
      console.log(`очередь listen ${backlog}, запросов ${calls.length}, отказов ${failed}`)
      expect(failed).toBe(0)
    }
    finally {
      service.kill()
      rmSync(directory, { recursive: true, force: true })
    }
  })
})

test.describe('control-expiry', () => {
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
})

test.describe('cron-lifecycle', () => {
  function harness(collect?: () => Promise<void>) {
    const clock = new FakeClock()
    const process = new EventEmitter()
    const calls: string[] = []
    const imports: Record<string, unknown> = {}
    imports['../utils/access-operations'] = {
      enqueueExpiredAccess: async () => { calls.push('enqueueExpiredAccess') },
      runAccessOperations: async () => { calls.push('runAccessOperations') },
    }
    imports['../utils/expiry'] = {
      reportControlExpiry: async () => { calls.push('reportControlExpiry') },
      syncControlExpiry: async () => { calls.push('syncControlExpiry') },
    }
    for (const [module, method] of [
      ['client-prefs', 'syncClientPrefsFile'],
      ['traffic-collector', 'collectTraffic'],
    ]) {
      imports[`../utils/${module}`] = { [method!]: async () => {
        calls.push(method!)
        if (method === 'collectTraffic') await collect?.()
      } }
    }
    const plugin = serverModule('server/plugins/cron.ts', imports, {
      defineNitroPlugin: (fn: unknown) => fn,
      useLogger: () => ({ error: () => {} }),
      setTimeout: clock.setTimeout,
      setInterval: clock.setInterval,
      clearTimeout: clock.clearTimeout,
      clearInterval: clock.clearInterval,
      process,
    }).default as (app: unknown) => void
    function start() {
      const closeHooks: Array<() => void | Promise<void>> = []
      plugin({ hooks: { hook: (name: string, fn: () => void | Promise<void>) => {
        expect(name).toBe('close')
        closeHooks.push(fn)
      } } })
      return async () => { for (const close of closeHooks) await close() }
    }
    return { clock, process, calls, start }
  }

  test('cron: закрытие до первого тика отменяет отложенный запуск и интервал', async () => {
    const h = harness()
    const close = h.start()
    await close()
    await h.clock.advance(180_000)
    expect(h.calls).toEqual([])
    expect(h.process.listenerCount('beforeExit')).toBe(0)
  })

  test('cron: медленный сбор не запускается повторно, закрытие дожидается записи', async () => {
    let finish!: () => void
    const held = new Promise<void>((resolve) => { finish = resolve })
    const h = harness(() => held)
    const close = h.start()
    await h.clock.advance(125_000)
    expect(h.calls.filter(call => call === 'collectTraffic')).toHaveLength(1)
    let closed = false
    const closing = close().then(() => { closed = true })
    await h.clock.flush()
    expect(closed).toBe(false)
    finish()
    await closing
    const completed = h.calls.length
    await h.clock.advance(120_000)
    expect(h.calls).toHaveLength(completed)
  })

  test('cron: после ошибки следующий интервал снова выполняет сбор', async () => {
    let runs = 0
    const h = harness(async () => {
      if (++runs === 1) throw new Error('temporary database failure')
    })
    const close = h.start()
    await h.clock.advance(65_000)
    expect(runs).toBe(2)
    await close()
  })

  test('cron: пересоздание Nitro оставляет только один работающий планировщик', async () => {
    const h = harness()
    for (let i = 0; i < 3; i++) {
      const close = h.start()
      await h.clock.advance(5_000)
      await close()
    }
    const close = h.start()
    h.calls.length = 0
    await h.clock.advance(65_000)
    expect(h.calls.filter(call => call === 'collectTraffic')).toHaveLength(2)
    await close()
    h.calls.length = 0
    await h.clock.advance(120_000)
    expect(h.calls).toEqual([])
    expect(h.process.listenerCount('beforeExit')).toBe(0)
  })

  test('исполнитель доступа ждёт готовности панели и завершает текущую попытку при закрытии', async () => {
    const clock = new FakeClock()
    let ready = false
    let calls = 0
    let finish!: () => void
    const held = new Promise<void>(resolve => { finish = resolve })
    let close!: () => Promise<void>
    const plugin = serverModule('server/plugins/access-operations.ts', {
      '../utils/access-operations': { runAccessOperations: async () => { calls++; await held } },
      '../utils/panel-ready': { isPanelReady: () => ready },
    }, {
      defineNitroPlugin: (fn: unknown) => fn,
      useLogger: () => ({ error: () => {} }),
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    }).default as (app: unknown) => void
    plugin({ hooks: { hook: (_name: string, fn: () => Promise<void>) => { close = fn } } })
    await clock.advance(3000)
    expect(calls).toBe(0)
    ready = true
    await clock.advance(3000)
    expect(calls).toBe(1)
    let closed = false
    const closing = close().then(() => { closed = true })
    await clock.flush()
    expect(closed).toBe(false)
    finish()
    await closing
    await clock.advance(3000)
    expect(calls).toBe(1)
  })
})
