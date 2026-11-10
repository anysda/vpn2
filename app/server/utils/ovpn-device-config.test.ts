import { beforeEach, describe, expect, it, vi } from 'vitest'
import { auth, calls, dbRows, fakeEvent, fetchMock, installStubs, rejection, stubError } from '../test/stubs'
import { deviceConfigName } from './naming'
import { buildOvpnConfig, caReady } from './openvpn'

vi.hoisted(() => vi.stubGlobal('defineEventHandler', (handler: unknown) => handler))
vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./bot-api', async () => (await import('../test/stubs')).botApiModule)
vi.mock('./openvpn', async () => {
  const { calls } = await import('../test/stubs')
  return {
    caReady: vi.fn(async () => { calls.push('caReady'); return true }),
    ensureDeviceOvpn: vi.fn(async () => { calls.push('ensure'); return { ovpnCert: 'CERT', ovpnKey: 'KEY' } }),
    buildOvpnConfig: vi.fn(async () => { calls.push('build'); return 'CONF' }),
  }
})

const { default: botConfig } = await import('../api/bot/devices/[deviceId]/ovpn-config.get')
const { default: adminConfig } = await import('../api/clients/[id]/devices/[deviceId]/ovpn-config.get')
const { default: sendOvpn } = await import('../api/clients/[id]/devices/[deviceId]/send-ovpn-to-tg.post')

type Handler = (event: never) => Promise<unknown>
const config = {
  ovpnEnabled: true, ovpnPublicHost: 'vpn.example', ovpnPort: '1194', ovpnProto: 'udp',
  wgSplitLocal: false, mgmtMeshIpPrefix: '10.99.0.0/24', tgbotSecret: 's3', tgbotEventPort: 9000,
}
const client = { id: 7, name: 'Анна', filterTraffic: true, tgChatId: 42 }
const device = { id: 3, clientId: 7, name: 'phone' }
const setHeader = vi.fn()

beforeEach(() => {
  installStubs(config)
  vi.stubGlobal('setHeader', setHeader)
  setHeader.mockReset()
  vi.mocked(caReady).mockClear()
  dbRows.clients = [client]
  dbRows.devices = [device]
})

const admin = (deviceId = '3') => fakeEvent({ id: '7', deviceId })
const bot = (deviceId = '3') => fakeEvent({ deviceId }, { chatId: '42' })
const cases = [
  { name: 'бот ovpn-config', run: botConfig as Handler, ok: bot, bad: fakeEvent({ deviceId: '3' }), invalid: 'invalid_params', authFail: auth.bot, lookups: ['db:clientByChat', 'db:select'], tail: ['ensure', 'build', 'db:chatLinked'] },
  { name: 'админ ovpn-config', run: adminConfig as Handler, ok: admin, bad: fakeEvent({ id: 'x', deviceId: '3' }), invalid: 'invalid_id', authFail: auth.admin, lookups: ['db:select', 'db:select'], tail: ['ensure', 'build'] },
  { name: 'админ send-ovpn-to-tg', run: sendOvpn as Handler, ok: admin, bad: fakeEvent({ id: '7', deviceId: 'x' }), invalid: 'invalid_id', authFail: auth.admin, lookups: ['db:select', 'db:select'], tail: ['ensure', 'build', 'fetch'] },
] as const

describe.each(cases)('$name', ({ run, ok, bad, invalid, authFail, lookups, tail }) => {
  it('отказ авторизации - первым', async () => {
    authFail.mockImplementation(() => { throw stubError({ statusCode: 401, statusMessage: 'unauthorized' }) })
    const err = await rejection(run(bad))
    expect(err.statusCode).toBe(401)
    expect(calls).toEqual(['auth'])
  })

  it(`плохие параметры - 400 ${invalid} до проверок OpenVPN`, async () => {
    installStubs({ ...config, ovpnEnabled: false })
    const err = await rejection(run(bad))
    expect([err.statusCode, err.statusMessage]).toEqual([400, invalid])
    expect(calls).toEqual(['auth'])
  })

  it('OpenVPN выключен - 503 до поиска в базе, даже когда девайса нет', async () => {
    installStubs({ ...config, ovpnEnabled: false, ovpnPublicHost: '' })
    const err = await rejection(run(ok('99')))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'ovpn_disabled', 'OpenVPN выключен'])
    expect(calls).toEqual(['auth'])
  })

  it('нет адреса - 500 до готовности CA и базы', async () => {
    installStubs({ ...config, ovpnPublicHost: '', tgbotSecret: '' })
    const err = await rejection(run(ok('99')))
    expect([err.statusCode, err.statusMessage]).toEqual([500, 'ovpn_public_host_not_configured'])
    expect(calls).toEqual(['auth'])
  })

  it('CA не готов - 503 до поиска в базе', async () => {
    vi.mocked(caReady).mockImplementationOnce(async () => { calls.push('caReady'); return false })
    const err = await rejection(run(ok('99')))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'ovpn_ca_not_ready', 'OpenVPN CA ещё не инициализирован (стадия 29-openvpn)'])
    expect(calls).toEqual(['auth', 'caReady'])
  })

  it('чужой или неизвестный девайс - 404 not_found до сборки', async () => {
    const err = await rejection(run(ok('99')))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect(calls).toEqual(['auth', 'caReady', ...lookups])
  })

  it('сборка из сертификата девайса и настроек; привязка чата - после неё', async () => {
    await run(ok())
    expect(calls).toEqual(['auth', 'caReady', ...lookups, ...tail])
    expect(vi.mocked(buildOvpnConfig)).toHaveBeenLastCalledWith({
      clientCert: 'CERT', clientKey: 'KEY', serverHost: 'vpn.example', serverPort: 1194, proto: 'udp',
      dns: ['10.99.0.1'], splitLocal: false, tunnelPrefixes: ['10.99.0.0/24'],
    })
  })
})

describe('ответ конфига', () => {
  it.each([['бот', botConfig, bot], ['админ', adminConfig, admin]] as const)('%s: текст конфига и тип text/plain', async (_name, run, event) => {
    installStubs({ ...config, tgbotSecret: '' })
    vi.stubGlobal('setHeader', setHeader)
    dbRows.clients = [client]
    dbRows.devices = [device]
    expect(await (run as Handler)(event())).toBe('CONF')
    expect(setHeader.mock.calls).toEqual([[expect.anything(), 'content-type', 'text/plain; charset=utf-8']])
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('send-ovpn-to-tg', () => {
  it('нет секрета бота - 503 до готовности CA, даже когда CA не готов', async () => {
    installStubs({ ...config, tgbotSecret: '' })
    vi.mocked(caReady).mockImplementationOnce(async () => { calls.push('caReady'); return false })
    const err = await rejection((sendOvpn as Handler)(admin('99')))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'bot_not_configured', 'Telegram-бот не настроен'])
    expect(calls).toEqual(['auth'])
    vi.mocked(caReady).mockReset()
    vi.mocked(caReady).mockImplementation(async () => { calls.push('caReady'); return true })
  })

  it('событие client_send_openvpn с именем файла и конфигом; ответ {ok: true}', async () => {
    expect(await (sendOvpn as Handler)(admin())).toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0]!
    expect(url).toBe('http://127.0.0.1:9000/event')
    expect(opts).toMatchObject({
      method: 'POST', headers: { 'X-Tgbot-Secret': 's3' }, timeout: 8000,
      body: { type: 'client_send_openvpn', name: deviceConfigName('Анна', 'phone', 3), conf: 'CONF' },
    })
    expect(Object.keys(opts.body as object)).toEqual(['type', 'name', 'conf'])
    expect(setHeader).not.toHaveBeenCalled()
  })

  it('бот не ответил - 502 bot_unreachable с причиной', async () => {
    const down = new Error('ECONNREFUSED')
    fetchMock.mockImplementation(async () => { throw down })
    const err = await rejection((sendOvpn as Handler)(admin()))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([502, 'bot_unreachable', 'Бот не отвечает — проверь /api/admin/telegram'])
    expect(err.cause).toBe(down)
  })
})
