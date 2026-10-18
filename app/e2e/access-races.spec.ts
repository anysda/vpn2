import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { hash, verify } from '@node-rs/argon2'
import { createClient } from '@libsql/client'
import { desc, eq } from 'drizzle-orm'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { expect, test } from '@playwright/test'
import * as panelSchema from '../server/database/schema'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'
import { serverModule } from './support/server-module'

// Гонки доступа на настоящих маршрутах и SQLite; подменены только VPN-службы,
// проверка входа и тело запроса.
const require = createRequire(import.meta.url)
const harness = require('./support/access-harness.cjs')
const root = process.env.VPN2_TEST_ROOT || path.resolve('..')

type Handler = (event: object) => Promise<unknown>
interface Outcome { status: number, body: Record<string, unknown> }

async function call(handler: Handler, event: object): Promise<Outcome> {
  try {
    return { status: 200, body: (await handler(event)) as Record<string, unknown> }
  }
  catch (error) {
    const { statusCode, statusMessage, message } = error as { statusCode?: number, statusMessage?: string, message: string }
    if (!statusCode) throw error
    return { status: statusCode, body: { statusMessage, message } }
  }
}

async function panel(runtime: Record<string, unknown> = {}) {
  const directory = mkdtempSync(path.join(tmpdir(), 'access-race-'))
  const app = harness(root, path.join(directory, 'db.sqlite'), {}, runtime)
  const schema: typeof Schema = app.load('app/server/database/schema.ts')
  const db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
  await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
  app.overrides.set('app/server/utils/validate.ts', {
    readBodyAs: async (event: { body: unknown }, body: { parse: (v: unknown) => unknown }) => body.parse(event.body),
  })
  app.overrides.set('app/server/utils/events.ts', { actorOf: () => 'panel', BOT_ACTOR: 'bot', recordEvent: async () => {}, joinDetail: () => '' })
  app.overrides.set('app/server/utils/auth.ts', { requireAuth: async () => ({ id: 1, username: 'admin' }) })
  app.overrides.set('app/server/utils/wireguard.ts', { ensureDeviceWg: async () => {}, syncWireguardConfig: async () => {} })
  app.overrides.set('app/server/utils/ikev2.ts', { ensureDeviceIkev2: async () => {}, syncIkev2: async () => {} })
  app.overrides.set('app/server/utils/client-prefs.ts', { syncClientPrefsFile: async () => {} })
  const route = (name: string): Handler => app.load(`app/server/api/${name}.ts`).default
  const useBot = () => {
    const real = app.load('app/server/utils/bot-api.ts')
    app.overrides.set('app/server/utils/bot-api.ts', { ...real, requireBotAuth: () => {} })
  }
  const close = () => {
    db.$client.close()
    rmSync(directory, { recursive: true, force: true })
  }
  return { app, schema, db, route, useBot, close }
}

/** caReady, на котором запросы стоят, пока тест не отпустит. */
function gate(expected: number) {
  let arrived = 0
  let all!: () => void
  let open!: () => void
  const waiting = new Promise<void>(resolve => { all = resolve })
  const opened = new Promise<void>(resolve => { open = resolve })
  return {
    caReady: async () => {
      if (++arrived === expected) all()
      await opened
      return true
    },
    waiting,
    open,
  }
}

test('VPN2-458: смена пароля в панели переживает перезапуск, смена в config.yaml применяется', async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'panel-password-'))
  const databaseUrl = `file:${path.join(directory, 'panel.sqlite')}`
  const client = createClient({ url: databaseUrl, concurrency: 1 })
  const db = drizzle(client, { schema: panelSchema })
  await migrate(db, { migrationsFolder: 'server/database/migrations' })
  const config = { admin: { user: 'admin', password: 'config-first-pass' } }
  const verifyPassword = async (hashed: string, plain: string) => {
    try {
      return await verify(hashed, plain)
    }
    catch {
      return false
    }
  }
  const restart = () => serverModule<{ default: (app: object) => Promise<void> }>('server/plugins/init.ts', {
    '@libsql/client': { createClient: () => client },
    '../database/client': { useDb: () => db },
    '../database/schema': panelSchema,
    '../utils/auth': { hashPassword: (plain: string) => hash(plain), verifyPassword, revokeDisallowedSsoSessions: async () => 0 },
    '../utils/anysda-config': { loadAnysdaConfig: () => config },
    '../utils/client-prefs': { syncClientPrefsFile: async () => {} },
    '../utils/control': { ControlUnavailable: class extends Error {} },
    '../utils/panel-ready': { markPanelReady: () => {} },
  }, {
    defineNitroPlugin: (callback: unknown) => callback,
    useRuntimeConfig: () => ({ databaseUrl }),
    useLogger: () => ({ info() {}, warn() {}, error() {}, fatal() {} }),
    process: Object.assign(Object.create(process), { exit: (code: number) => { throw new Error(`init exit ${code}`) } }),
    console,
  }).default({ hooks: { hook() {} } })
  const credential = async () => {
    const [row] = await db.select().from(panelSchema.accounts).where(eq(panelSchema.accounts.providerId, 'credential'))
    return row!.password!
  }
  try {
    await restart()
    const [admin] = await db.select().from(panelSchema.users)
    // Смена в панели: better-auth пишет только хеш учётки, сессия остаётся.
    await db.update(panelSchema.accounts).set({ password: await hash('panel-new-pass') })
      .where(eq(panelSchema.accounts.providerId, 'credential'))
    await db.insert(panelSchema.sessions).values({ token: 'live-session', userId: admin!.id, expiresAt: new Date(Date.now() + 86_400_000) })

    await restart()
    const afterRestart = {
      panel: await verifyPassword(await credential(), 'panel-new-pass'),
      config: await verifyPassword(await credential(), 'config-first-pass'),
      sessions: (await db.select().from(panelSchema.sessions)).length,
    }
    console.log('перезапуск с прежним config.yaml:', afterRestart)
    expect(afterRestart).toEqual({ panel: true, config: false, sessions: 1 })

    config.admin.password = 'config-second-pass'
    await restart()
    const afterDeploy = {
      config: await verifyPassword(await credential(), 'config-second-pass'),
      panel: await verifyPassword(await credential(), 'panel-new-pass'),
      sessions: (await db.select().from(panelSchema.sessions)).length,
    }
    console.log('перезапуск с новым admin.password:', afterDeploy)
    expect(afterDeploy).toEqual({ config: true, panel: false, sessions: 0 })
  }
  finally {
    client.close()
    rmSync(directory, { recursive: true, force: true })
  }
})

test('VPN2-461: сохранение списка SSO отзывает сессии выпавших, выключение SSO — все SSO-сессии', async () => {
  const { app, schema, db, route, close } = await panel({ session: { password: 'test-only-session-secret-0123456789', cookie: { secure: false } } })
  try {
    const realSso = app.load('app/server/utils/sso.ts')
    let stored = {
      enabled: true, authentikUrl: 'https://idp.example.test', appSlug: 'vpn2', clientId: 'panel', clientSecret: 'test-secret',
      allowedSubs: 'sub-admin:admin,sub-bob:bob', label: 'Authentik', autoRedirect: false, passwordLogin: true,
      redirectUrl: 'https://panel.example.test/auth/authentik',
    }
    const settings = () => {
      const discoveryUrl = realSso.discoveryFrom(stored.authentikUrl, stored.appSlug)
      const enabled = stored.enabled && !!stored.clientId && !!discoveryUrl && !!stored.clientSecret
      return {
        enabled, autoRedirect: enabled && stored.autoRedirect, passwordLogin: !enabled || stored.passwordLogin, label: stored.label,
        allowed: realSso.parseAllowedSubs(stored.allowedSubs), discoveryUrl,
        clientId: stored.clientId, clientSecret: stored.clientSecret, redirectUrl: stored.redirectUrl,
      }
    }
    app.overrides.set('app/server/utils/sso.ts', {
      ...realSso,
      readSsoStored: () => ({ stored: { ...stored }, fromPanel: true }),
      writeSsoStored: (next: typeof stored) => { stored = { ...next } },
      ssoSettings: settings,
    })
    app.overrides.delete('app/server/utils/auth.ts')
    const auth = app.load('app/server/utils/auth.ts')
    const ssoPut = route('admin/sso.put')

    for (const username of ['admin', 'bob']) {
      const [user] = await db.insert(schema.users).values({ username, name: username, email: `${username}@panel.invalid` }).returning()
      await db.insert(schema.accounts).values([
        { accountId: String(user!.id), providerId: 'credential', userId: user!.id, password: await hash(`${username}-password`) },
        { accountId: `sub-${username}`, providerId: 'authentik', userId: user!.id },
      ])
    }
    const login = async (username: string, method: 'password' | 'sso') => {
      const { headers } = await auth.useAuth().api.signInUsername({
        body: { username, password: `${username}-password` }, headers: new Headers({ host: 'panel.test' }), returnHeaders: true,
      })
      const [session] = await db.select().from(schema.sessions).orderBy(desc(schema.sessions.id)).limit(1)
      // Вход через Authentik: та же строка сессии, отмеченная как SSO.
      await db.update(schema.sessions).set({ loginMethod: method }).where(eq(schema.sessions.id, session!.id))
      return new Headers({ host: 'panel.test', cookie: String(headers.get('set-cookie')).split(';')[0]! })
    }
    const adminPassword = await login('admin', 'password')
    const adminSso = await login('admin', 'sso')
    const bobSso = await login('bob', 'sso')
    const status = async (headers: Headers) => (await call(auth.requireAuth, { headers })).status
    expect([await status(adminPassword), await status(adminSso), await status(bobSso)]).toEqual([200, 200, 200])

    const removed = await call(ssoPut, { headers: adminPassword, body: { ...stored, allowedSubs: 'sub-admin:admin' } })
    const afterRemove = { save: removed.status, bob: await status(bobSso), adminSso: await status(adminSso), adminPassword: await status(adminPassword) }
    console.log('bob убран из списка SSO:', afterRemove)
    expect(afterRemove).toEqual({ save: 200, bob: 401, adminSso: 200, adminPassword: 200 })

    const disabled = await call(ssoPut, { headers: adminPassword, body: { ...stored, enabled: false } })
    const afterDisable = { save: disabled.status, adminSso: await status(adminSso), adminPassword: await status(adminPassword) }
    console.log('SSO выключено:', afterDisable)
    expect(afterDisable).toEqual({ save: 200, adminSso: 401, adminPassword: 200 })
  }
  finally {
    close()
  }
})

test('VPN2-462: лимит, сниженный во время двух параллельных POST, пропускает ровно одно устройство', async () => {
  const { app, schema, db, route, close } = await panel({ ovpnEnabled: true })
  try {
    const paused = gate(2)
    app.overrides.set('app/server/utils/openvpn.ts', { caReady: paused.caReady, ensureDeviceOvpn: async () => {} })
    app.overrides.set('app/server/utils/bot-events.ts', { notifyClient: async () => {} })
    const post = route('clients/[id]/devices/index.post')
    const [client] = await db.insert(schema.clients).values({ name: 'limit', password: 'limit-invite', deviceLimit: 3 }).returning()
    const requests = ['phone', 'laptop'].map(name => call(post, { params: { id: String(client!.id) }, body: { name } }))
    await paused.waiting
    // Оба запроса уже прочитали лимит 3; администратор снижает его до 1.
    await db.update(schema.clients).set({ deviceLimit: 1 }).where(eq(schema.clients.id, client!.id))
    paused.open()
    const results = await Promise.all(requests)
    const devices = await db.select().from(schema.devices).where(eq(schema.devices.clientId, client!.id))
    console.log('параллельные POST при лимите 1:', results.map(r => `${r.status} ${r.body.statusMessage ?? ''}`.trim()), `устройств ${devices.length}`)
    expect(results.map(r => r.status).sort()).toEqual([200, 409])
    expect(results.find(r => r.status === 409)!.body.statusMessage).toBe('device_limit')
    expect(devices).toHaveLength(1)
  }
  finally {
    close()
  }
})

test('VPN2-467: при полной очереди бота отзыв Telegram проходит, старое приглашение не принимается', async () => {
  const { schema, db, route, useBot, close } = await panel()
  try {
    useBot()
    const unlink = route('clients/[id]/unlink.post')
    const link = route('bot/link.post')
    const [client] = await db.insert(schema.clients).values({ name: 'queue', password: 'old-invite-password', tgChatId: 900, tgUsername: 'queue_user' }).returning()
    const now = Date.now()
    await db.insert(schema.botOutbox).values(Array.from({ length: 1024 }, (_, i) => ({
      id: `queued-${i}`, label: 'test', payload: '{}', status: 'queued' as const, due: now, expires: now + 86_400_000, created: now,
    })))
    const result = await call(unlink, { params: { id: String(client!.id) } })
    const [after] = await db.select().from(schema.clients).where(eq(schema.clients.id, client!.id))
    const relink = await call(link, { body: { password: 'old-invite-password', chatId: 900 } })
    console.log('unlink при 1024 в очереди:', result, {
      tgChatId: after!.tgChatId, tgUsername: after!.tgUsername, passwordChanged: after!.password !== 'old-invite-password',
    }, 'старое приглашение:', relink.status, relink.body.statusMessage)
    expect(result.status).toBe(200)
    expect(String(result.body.warning)).toContain('очередь заполнена')
    expect(after).toMatchObject({ tgChatId: null, tgUsername: null })
    expect(after!.password).not.toBe('old-invite-password')
    expect(relink).toMatchObject({ status: 404, body: { statusMessage: 'invalid_password' } })
  }
  finally {
    close()
  }
})

test('VPN2-468: отзыв между проверкой приглашения и записью привязки побеждает', async () => {
  const { app, schema, db, route, useBot, close } = await panel()
  try {
    app.overrides.set('app/server/utils/bot-events.ts', { notifyClient: async () => {} })
    useBot()
    // Первый select после взвода отдаёт результат только после отзыва:
    // так link проверил пароль, а записать привязку ещё не успел.
    let interleave: (() => Promise<void>) | null = null
    const hooked = <T extends object>(builder: T, run: () => Promise<void>): T => new Proxy(builder, {
      get(target, key) {
        const value = Reflect.get(target, key, target)
        if (key === 'then') {
          return (ok: (v: unknown) => unknown, fail: (e: unknown) => unknown) =>
            value.call(target, async (rows: unknown) => { await run(); return rows }).then(ok, fail)
        }
        if (typeof value !== 'function') return value
        return (...args: unknown[]) => {
          const out = value.apply(target, args)
          return out && typeof out === 'object' && 'then' in out ? hooked(out, run) : out
        }
      },
    })
    const wrapped = new Proxy(db, {
      get(target, key) {
        const value = Reflect.get(target, key, target)
        if (key === 'select' && interleave) {
          const run = interleave
          interleave = null
          return (...args: unknown[]) => hooked(value.apply(target, args), run)
        }
        return typeof value === 'function' ? value.bind(target) : value
      },
    })
    app.overrides.set('app/server/database/client.ts', { useDb: () => wrapped })
    const unlink = route('clients/[id]/unlink.post')
    const link = route('bot/link.post')
    const [client] = await db.insert(schema.clients).values({ name: 'race', password: 'invite-before-revoke' }).returning()

    let revoked: Outcome | null = null
    interleave = async () => { revoked = await call(unlink, { params: { id: String(client!.id) } }) }
    const linked = await call(link, { body: { password: 'invite-before-revoke', chatId: 777 } })
    const [after] = await db.select().from(schema.clients).where(eq(schema.clients.id, client!.id))
    console.log('link с отзывом между проверкой и записью:', { unlink: revoked?.status, link: linked.status, tgChatId: after!.tgChatId })
    expect(revoked).toMatchObject({ status: 200 })
    expect(linked).toMatchObject({ status: 404, body: { statusMessage: 'invalid_password' } })
    expect(after!.tgChatId).toBeNull()
  }
  finally {
    close()
  }
})

test('VPN2-468: отзыв бота во время добавления устройства через бота не создаёт устройство', async () => {
  const { app, schema, db, route, useBot, close } = await panel({ ovpnEnabled: true })
  try {
    const paused = gate(1)
    app.overrides.set('app/server/utils/openvpn.ts', { caReady: paused.caReady, ensureDeviceOvpn: async () => {} })
    app.overrides.set('app/server/utils/bot-events.ts', { notifyClient: async () => {} })
    useBot()
    const add = route('bot/devices/index.post')
    const unlink = route('clients/[id]/unlink.post')
    const [client] = await db.insert(schema.clients).values({ name: 'bot-add', password: 'bot-add-invite', tgChatId: 555 }).returning()
    const request = call(add, { body: { chatId: 555, name: 'phone' } })
    await paused.waiting
    const revoked = await call(unlink, { params: { id: String(client!.id) } })
    paused.open()
    const result = await request
    const devices = await db.select().from(schema.devices).where(eq(schema.devices.clientId, client!.id))
    console.log('бот добавляет устройство, бот отозван:', { unlink: revoked.status, add: result.status, statusMessage: result.body.statusMessage, devices: devices.length })
    expect(revoked.status).toBe(200)
    expect(result).toMatchObject({ status: 404, body: { statusMessage: 'not_linked' } })
    expect(devices).toHaveLength(0)
  }
  finally {
    close()
  }
})

test('VPN2-476: отзыв бота во время удаления и перевыпуска через бота не меняет устройства', async () => {
  const { app, schema, db, route, useBot, close } = await panel()
  try {
    // 50 запросов бота стоят между поиском клиента по чату и записью, пока идёт отзыв.
    const paused = gate(50)
    app.overrides.set('app/server/utils/access-adapter.ts', { accessAdapter: {
      enabled: async () => { await paused.caReady(); return { wg: true, openvpn: false, ikev2: true } },
    } })
    app.overrides.set('app/server/utils/bot-events.ts', { notifyClient: async () => {} })
    useBot()
    const remove = route('bot/devices/[deviceId].delete')
    const reissue = route('bot/devices/[deviceId]/reissue.post')
    const unlink = route('clients/[id]/unlink.post')
    const [client] = await db.insert(schema.clients).values({ name: 'bot-ops', password: 'bot-ops-invite', tgChatId: 556 }).returning()
    const created = await db.insert(schema.devices).values(Array.from({ length: 5 }, (_, i) => ({
      clientId: client!.id, name: `dev-${i}`, wgPrivateKey: `priv-${i}`, wgPublicKey: `pub-${i}`, wgPresharedKey: `psk-${i}`, ikev2Password: `ike-${i}`,
    }))).returning()
    const requests = Array.from({ length: 50 }, (_, i) => {
      const deviceId = String(created[i % created.length]!.id)
      return i % 2
        ? call(reissue, { params: { deviceId }, body: { chatId: 556 } })
        : call(remove, { params: { deviceId }, query: { chatId: '556' } })
    })
    await paused.waiting
    const revoked = await call(unlink, { params: { id: String(client!.id) } })
    paused.open()
    const results = await Promise.all(requests)
    const after = await db.select().from(schema.devices).where(eq(schema.devices.clientId, client!.id))
    const operations = await db.select().from(schema.accessOperations).where(eq(schema.accessOperations.clientId, client!.id))
    const statuses = results.reduce<Record<string, number>>((acc, r) => {
      const label = `${r.status} ${r.body.statusMessage ?? ''}`.trim()
      acc[label] = (acc[label] ?? 0) + 1
      return acc
    }, {})
    console.log('бот удаляет и перевыпускает, бот отозван:', { unlink: revoked.status, results: statuses, operations: operations.length })
    expect(revoked.status).toBe(200)
    expect(statuses).toEqual({ '404 not_linked': 50 })
    expect(operations).toHaveLength(0)
    expect(after.map(({ id, deleting, wgPublicKey, wgPrivateKey, wgPresharedKey, ikev2Password }) =>
      ({ id, deleting, wgPublicKey, wgPrivateKey, wgPresharedKey, ikev2Password })))
      .toEqual(created.map(({ id, deleting, wgPublicKey, wgPrivateKey, wgPresharedKey, ikev2Password }) =>
        ({ id, deleting, wgPublicKey, wgPrivateKey, wgPresharedKey, ikev2Password })))

    // Привязанный чат те же маршруты пропускают.
    const [kept] = await db.insert(schema.clients).values({ name: 'bot-kept', password: 'bot-kept-invite', tgChatId: 557 }).returning()
    const [phone, laptop] = await db.insert(schema.devices).values([
      { clientId: kept!.id, name: 'phone', wgPublicKey: 'kept-pub-1' },
      { clientId: kept!.id, name: 'laptop', wgPublicKey: 'kept-pub-2' },
    ]).returning()
    const linkedRequests = Promise.all([
      call(reissue, { params: { deviceId: String(phone!.id) }, body: { chatId: 557 } }),
      call(remove, { params: { deviceId: String(laptop!.id) }, query: { chatId: '557' } }),
    ])
    paused.open()
    const linked = await linkedRequests
    const [phoneAfter] = await db.select().from(schema.devices).where(eq(schema.devices.id, phone!.id))
    const [laptopAfter] = await db.select().from(schema.devices).where(eq(schema.devices.id, laptop!.id))
    console.log('привязанный чат:', linked.map(r => r.status), { reissued: phoneAfter!.wgPublicKey !== 'kept-pub-1', deleting: laptopAfter!.deleting })
    expect(linked.map(r => r.status)).toEqual([200, 200])
    expect(phoneAfter!.wgPublicKey).not.toBe('kept-pub-1')
    expect(laptopAfter!.deleting).toBe(true)
  }
  finally {
    close()
  }
})
