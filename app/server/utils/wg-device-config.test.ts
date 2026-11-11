import { beforeEach, describe, expect, it, vi } from 'vitest'
import { auth, calls, dbRows, fakeEvent, fetchMock, installStubs, rejection, stubError } from '../test/stubs'
import devices from '../resources/devices'
import { deviceConfigName } from './naming'
import { qrPng, qrSvg } from './qr'

const wg = vi.hoisted(() => {
  (globalThis as Record<string, unknown>).defineEventHandler = (handler: unknown) => handler
  return { keysReady: true, syncFails: false, params: undefined as unknown }
})

const botWgConfig = devices['GET /bot/devices/:deviceId/wg-config']
const botWgQrPng = devices['GET /bot/devices/:deviceId/wg-qrcode.png']
const adminWgConfig = devices['GET /clients/:id/devices/:deviceId/wg-config']
const adminWgQrPng = devices['GET /clients/:id/devices/:deviceId/wg-qrcode.png']
const adminWgQrSvg = devices['GET /clients/:id/devices/:deviceId/wg-qrcode.svg']
const sendWgToTg = devices['POST /clients/:id/devices/:deviceId/send-wg-to-tg']

vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./bot-api', async () => (await import('../test/stubs')).botApiModule)
vi.mock('./wireguard', async () => {
  const { calls } = await import('../test/stubs')
  return {
    loadServerKeys: async () => {
      calls.push('wg:keys')
      if (!wg.keysReady) throw new Error('no server keys')
      return { publicKey: 'SERVERPUB' }
    },
    ensureDeviceWg: async (id: number) => {
      calls.push(`wg:device:${id}`)
      return { wgPrivateKey: 'priv', wgPresharedKey: 'psk', wgIp: '10.8.0.5' }
    },
    syncWireguardConfig: async () => {
      calls.push('wg:sync')
      if (wg.syncFails) throw new Error('sync failed')
    },
    buildWgClientConfig: (p: Record<string, unknown>) => {
      calls.push('wg:build')
      wg.params = p
      return `[Interface]\nAddress = ${p.clientIp}\nEndpoint = ${p.serverEndpoint}\n`
    },
  }
})

const config = {
  wgEnabled: true, wgPublicHost: 'vpn.example.org', wgListenPort: 51820, wgMtu: 1380, wgSplitLocal: true,
  wgSubnetPrefix: '10.8.0', mgmtMeshIpPrefix: '10.99', tgbotSecret: 's3cret', tgbotEventPort: 8877,
}
const conf = '[Interface]\nAddress = 10.8.0.5\nEndpoint = vpn.example.org\n'
const params = {
  clientPrivateKey: 'priv', clientPresharedKey: 'psk', clientIp: '10.8.0.5', serverPublicKey: 'SERVERPUB',
  serverEndpoint: 'vpn.example.org', serverPort: 51820, dns: '10.99.0.1', mtu: 1380, splitLocal: true,
  tunnelPrefixes: ['10.8.0', '10.99'],
}
const core = ['wg:keys', 'wg:device:3', 'wg:sync', 'wg:build']
const headers: Record<string, string> = {}

beforeEach(() => setup(config))

function setup(runtime: Record<string, unknown>) {
  installStubs(runtime)
  wg.keysReady = true
  wg.syncFails = false
  wg.params = undefined
  for (const key of Object.keys(headers)) Reflect.deleteProperty(headers, key)
  vi.stubGlobal('setHeader', (_event: unknown, name: string, value: string) => { headers[name] = value })
  dbRows.clients = [{ id: 7, name: 'Анна', tgChatId: 42, filterTraffic: true }, { id: 8, name: 'Борис', tgChatId: 43, filterTraffic: false }]
  dbRows.devices = [{ id: 3, clientId: 7, name: 'phone' }, { id: 4, clientId: 8, name: 'laptop' }]
}

type Handler = (event: never) => unknown
const bot = { ok: fakeEvent({ deviceId: '3' }, { chatId: '42' }), bad: fakeEvent({ deviceId: '3' }, {}), foreign: fakeEvent({ deviceId: '4' }, { chatId: '42' }), invalid: 'invalid_params', authFail: auth.bot, lookups: ['db:clientByChat', 'db:select'] }
const admin = { ok: fakeEvent({ id: '7', deviceId: '3' }), bad: fakeEvent({ id: 'x', deviceId: '3' }), foreign: fakeEvent({ id: '7', deviceId: '4' }), invalid: 'invalid_id', authFail: auth.admin, lookups: ['db:select', 'db:select'] }
const text = 'text/plain; charset=utf-8'

const routes = [
  { name: 'бот wg-config', handler: botWgConfig, ...bot, tail: ['db:chatLinked'], type: text, body: () => conf },
  { name: 'бот wg-qrcode.png', handler: botWgQrPng, ...bot, tail: ['db:chatLinked'], type: 'image/png', body: () => qrPng(conf) },
  { name: 'админ wg-config', handler: adminWgConfig, ...admin, tail: [], type: text, body: () => conf },
  { name: 'админ send-wg-to-tg', handler: sendWgToTg, ...admin, tail: ['fetch'], type: undefined, body: () => ({ ok: true }) },
  { name: 'админ wg-qrcode.png', handler: adminWgQrPng, ...admin, tail: [], type: 'image/png', body: () => qrPng(conf) },
  { name: 'админ wg-qrcode.svg', handler: adminWgQrSvg, ...admin, tail: [], type: 'image/svg+xml; charset=utf-8', body: () => qrSvg(conf) },
] as const

const run = (handler: Handler, event: never) => Promise.resolve().then(() => handler(event))

describe.each(routes)('$name', ({ handler, ok, bad, foreign, invalid, authFail, lookups, tail, type, body }) => {
  it('отказ авторизации - первым, до 400 и проверок WireGuard', async () => {
    authFail.mockImplementation(() => { throw stubError({ statusCode: 401, statusMessage: 'unauthorized' }) })
    const err = await rejection(run(handler, bad))
    expect(err.statusCode).toBe(401)
    expect(calls).toEqual(['auth'])
  })

  it(`плохие параметры - 400 ${invalid} до проверок WireGuard`, async () => {
    setup({ ...config, wgEnabled: false })
    const err = await rejection(run(handler, bad))
    expect([err.statusCode, err.statusMessage]).toEqual([400, invalid])
    expect(calls).toEqual(['auth'])
  })

  it.each(['1.5', '-1'])(`номер девайса %s - 400 ${invalid} до проверок WireGuard`, async (deviceId) => {
    setup({ ...config, wgEnabled: false })
    const event = invalid === 'invalid_id' ? fakeEvent({ id: '7', deviceId }) : fakeEvent({ deviceId }, { chatId: '42' })
    const err = await rejection(run(handler, event))
    expect([err.statusCode, err.statusMessage]).toEqual([400, invalid])
    expect(calls).toEqual(['auth'])
  })

  it('WireGuard выключен - 503 wg_disabled до поиска, даже для чужого девайса', async () => {
    setup({ ...config, wgEnabled: false })
    const err = await rejection(run(handler, foreign))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'wg_disabled', 'WireGuard выключен'])
    expect(calls).toEqual(['auth'])
  })

  it('адрес не задан - 500 wg_public_host_not_configured до поиска', async () => {
    setup({ ...config, wgPublicHost: '' })
    const err = await rejection(run(handler, foreign))
    expect([err.statusCode, err.statusMessage]).toEqual([500, 'wg_public_host_not_configured'])
    expect(calls).toEqual(['auth'])
  })

  it('чужой девайс - 404 not_found до ядра', async () => {
    const err = await rejection(run(handler, foreign))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect(calls).toEqual(['auth', ...lookups])
  })

  it('ключи сервера не готовы - 503 wg_keys_not_ready после поиска', async () => {
    wg.keysReady = false
    const err = await rejection(run(handler, ok))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'wg_keys_not_ready', 'WireGuard server-ключи ещё не инициализированы (стадия 28-wireguard)'])
    expect(calls).toEqual(['auth', ...lookups, 'wg:keys'])
  })

  it('ответ: ядро, затем хвост маршрута; конфиг из параметров клиента', async () => {
    expect(await run(handler, ok)).toEqual(body())
    expect(calls).toEqual(['auth', ...lookups, ...core, ...tail])
    expect(wg.params).toEqual(params)
    expect(headers['content-type']).toBe(type)
  })

  it('синхронизация отклонена ошибкой - конфиг всё равно собран, ответ тот же', async () => {
    wg.syncFails = true
    expect(await run(handler, ok)).toEqual(body())
    expect(calls).toEqual(['auth', ...lookups, ...core, ...tail])
    expect(wg.params).toEqual(params)
  })

  it('wgSplitLocal: false - в сборку уходит splitLocal false', async () => {
    setup({ ...config, wgSplitLocal: false })
    await run(handler, ok)
    expect(wg.params).toEqual({ ...params, splitLocal: false })
  })

  it('wgSplitLocal не задан - splitLocal true', async () => {
    const { wgSplitLocal: _unset, ...noSplit } = config
    setup(noSplit)
    await run(handler, ok)
    expect(wg.params).toEqual({ ...params, splitLocal: true })
  })
})

describe('админ send-wg-to-tg: секрет бота и событие', () => {
  it('без секрета - 503 bot_not_configured до поиска, даже для чужого id', async () => {
    setup({ ...config, tgbotSecret: '' })
    const err = await rejection(run(sendWgToTg, admin.foreign))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'bot_not_configured', 'Telegram-бот не настроен'])
    expect(calls).toEqual(['auth'])
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('без секрета и с выключенным WireGuard - первым wg_disabled', async () => {
    setup({ ...config, tgbotSecret: '', wgEnabled: false })
    expect((await rejection(run(sendWgToTg, admin.ok))).statusMessage).toBe('wg_disabled')
  })

  it('конфиг без секрета бота отдаётся: секрет нужен только отправке', async () => {
    setup({ ...config, tgbotSecret: '' })
    expect(await run(adminWgConfig, admin.ok)).toBe(conf)
    expect(await run(botWgConfig, bot.ok)).toBe(conf)
  })

  it('тело client_send_wireguard: имя конфига, сам конфиг и его PNG для бота, подпись секретом', async () => {
    await run(sendWgToTg, admin.ok)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0]!
    expect(url).toBe('http://127.0.0.1:8877/event')
    expect(opts).toMatchObject({ method: 'POST', headers: { 'X-Tgbot-Secret': 's3cret' }, timeout: 8000 })
    expect(opts.body).toEqual({ type: 'client_send_wireguard', name: deviceConfigName('Анна', 'phone', 3), conf, qr: qrPng(conf).toString('base64') })
    expect(Object.keys(opts.body as object)).toEqual(['type', 'name', 'conf', 'qr'])
  })

  it('бот не ответил - 502 bot_unreachable с причиной', async () => {
    const cause = new Error('ECONNREFUSED')
    fetchMock.mockImplementation(async () => { throw cause })
    const err = await rejection(run(sendWgToTg, admin.ok))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([502, 'bot_unreachable', 'Бот не отвечает — проверь /api/admin/telegram'])
    expect(err.cause).toBe(cause)
  })
})

it('без фильтра трафика - DNS 8.8.8.8, 1.1.1.1', async () => {
  dbRows.clients!.push({ id: 9, name: 'Вера', tgChatId: 44, filterTraffic: false })
  dbRows.devices!.push({ id: 3, clientId: 9, name: 'tab' })
  dbRows.devices = dbRows.devices!.filter(d => d.clientId !== 7)
  await run(botWgConfig, fakeEvent({ deviceId: '3' }, { chatId: '44' }))
  expect(wg.params).toMatchObject({ dns: '8.8.8.8, 1.1.1.1' })
})
