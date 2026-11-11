import { createError as h3Error } from 'h3'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { auth, calls, dbRows, fakeEvent, fetchMock, installStubs, rejection, stubError } from '../test/stubs'
import { buildIkev2ClientInfo, entryHost, ikev2ServerReady } from './ikev2'

vi.hoisted(() => vi.stubGlobal('defineEventHandler', (handler: unknown) => handler))
vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))
vi.mock('./ikev2', async (importOriginal) => {
  const { calls } = await import('../test/stubs')
  return {
    ...await importOriginal<typeof import('./ikev2')>(),
    ikev2ServerReady: vi.fn(async () => { calls.push('ready'); return true }),
    buildIkev2ClientInfo: vi.fn(async () => { calls.push('build'); return { ...info } }),
  }
})

const info = {
  server: '203.0.113.7', remoteId: '203.0.113.7', username: 'anna-phone', password: 'P4ss',
  caCertPem: 'CA', certificatePending: false,
}
const credentials = (await import('../resources/devices')).default['GET /clients/:id/devices/:deviceId/ikev2-credentials']
const sendIkev2 = (await import('../resources/devices')).default['POST /clients/:id/devices/:deviceId/send-ikev2-to-tg']

type Handler = (event: never) => Promise<unknown>
const config = { tgbotSecret: 's3', tgbotEventPort: 9000, wgPublicHost: '' }
const admin = (deviceId = '3') => fakeEvent({ id: '7', deviceId })
const notReady = () => vi.mocked(ikev2ServerReady).mockImplementationOnce(async () => { calls.push('ready'); return false })

beforeEach(() => {
  installStubs(config)
  vi.unstubAllEnvs()
  vi.stubEnv('ENTRY_HOST', '198.51.100.9')
  vi.mocked(buildIkev2ClientInfo).mockClear()
  vi.mocked(ikev2ServerReady).mockReset()
  vi.mocked(ikev2ServerReady).mockImplementation(async () => { calls.push('ready'); return true })
  dbRows.clients = [{ id: 7, name: 'Анна' }]
  dbRows.devices = [{ id: 3, clientId: 7, name: 'phone' }]
})

const cases = [
  { name: 'ikev2-credentials', run: credentials as Handler, tail: ['build'] },
  { name: 'send-ikev2-to-tg', run: sendIkev2 as Handler, tail: ['build', 'fetch'] },
] as const

describe.each(cases)('$name', ({ run, tail }) => {
  it('отказ авторизации - первым', async () => {
    auth.admin.mockImplementation(() => { throw stubError({ statusCode: 401, statusMessage: 'unauthorized' }) })
    const err = await rejection(run(fakeEvent({ id: 'x', deviceId: '3' })))
    expect(err.statusCode).toBe(401)
    expect(calls).toEqual(['auth'])
  })

  it.each([[{ id: 'x', deviceId: '3' }], [{ id: '7', deviceId: 'x' }]])('плохой id %o - 400 invalid_id до готовности IKEv2', async (params) => {
    notReady()
    const err = await rejection(run(fakeEvent(params)))
    expect([err.statusCode, err.statusMessage]).toEqual([400, 'invalid_id'])
    expect(calls).toEqual(['auth'])
  })

  it('IKEv2 не развёрнут - 503 до секрета, адреса и базы', async () => {
    installStubs({ ...config, tgbotSecret: '' })
    vi.stubEnv('ENTRY_HOST', '')
    notReady()
    const err = await rejection(run(admin('99')))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'ikev2_not_deployed', 'IKEv2-сервер ещё не развёрнут (стадия 27-ikev2)'])
    expect(calls).toEqual(['auth', 'ready'])
  })

  it('нет ENTRY_HOST и wgPublicHost - 500 до поиска в базе', async () => {
    vi.stubEnv('ENTRY_HOST', ' ')
    const err = await rejection(run(admin('99')))
    expect([err.statusCode, err.statusMessage]).toEqual([500, 'entry_host_not_configured'])
    expect(calls).toEqual(['auth', 'ready'])
  })

  it('пустой ENTRY_HOST - адрес из wgPublicHost, ошибки нет', async () => {
    installStubs({ ...config, wgPublicHost: '203.0.113.50' })
    vi.stubEnv('ENTRY_HOST', '')
    dbRows.clients = [{ id: 7, name: 'Анна' }]
    dbRows.devices = [{ id: 3, clientId: 7, name: 'phone' }]
    await run(admin())
    expect(calls).toEqual(['auth', 'ready', 'db:select', 'db:select', ...tail])
  })

  it.each([['чужой клиент', '9', '3'], ['чужой девайс', '7', '99']])('%s - 404 not_found до сборки', async (_n, id, deviceId) => {
    const err = await rejection(run(fakeEvent({ id, deviceId })))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect(calls.at(-1)).toBe('db:select')
    expect(calls.slice(0, 2)).toEqual(['auth', 'ready'])
  })

  it('ядро после поиска: сборка по id девайса', async () => {
    await run(admin())
    expect(calls).toEqual(['auth', 'ready', 'db:select', 'db:select', ...tail])
    expect(vi.mocked(buildIkev2ClientInfo).mock.lastCall![0]).toBe(3)
  })
})

describe('ikev2-credentials', () => {
  it('без секрета бота отдаёт поля IKEv2, бот не вызывается', async () => {
    installStubs({ ...config, tgbotSecret: '' })
    dbRows.clients = [{ id: 7, name: 'Анна' }]
    dbRows.devices = [{ id: 3, clientId: 7, name: 'phone' }]
    expect(await (credentials as Handler)(admin())).toEqual(info)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('send-ikev2-to-tg', () => {
  const send = sendIkev2 as Handler

  it('нет секрета бота - 503 после готовности IKEv2 и до ENTRY_HOST и базы', async () => {
    installStubs({ ...config, tgbotSecret: '' })
    vi.stubEnv('ENTRY_HOST', '')
    const err = await rejection(send(admin('99')))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'bot_not_configured', 'Telegram-бот не настроен'])
    expect(calls).toEqual(['auth', 'ready'])
  })

  it('событие client_send_ikev2 с полями IKEv2; ответ {ok: true}', async () => {
    expect(await send(admin())).toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0]!
    expect(url).toBe('http://127.0.0.1:9000/event')
    expect(opts).toMatchObject({ method: 'POST', headers: { 'X-Tgbot-Secret': 's3' }, timeout: 8000 })
    expect(opts.retry ?? 0).toBe(0)
    expect(opts.body).toEqual({
      type: 'client_send_ikev2', clientName: 'Анна', username: 'anna-phone', server: '203.0.113.7', password: 'P4ss', caCertPem: 'CA',
    })
    expect(Object.keys(opts.body as object)).toEqual(['type', 'clientName', 'username', 'server', 'password', 'caCertPem'])
  })

  it('бот не ответил - 502 bot_unreachable; причина в тело ответа h3 не попадает', async () => {
    fetchMock.mockImplementation(async () => { throw new Error('ECONNREFUSED') })
    const err = await rejection(send(admin()))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([502, 'bot_unreachable', 'Бот не отвечает — проверь /api/admin/telegram'])
    const fields = { statusCode: err.statusCode, statusMessage: err.statusMessage, message: err.message }
    const withCause = h3Error({ ...fields, cause: new Error('ECONNREFUSED') })
    expect(withCause.cause).toBeDefined()
    expect(JSON.stringify(withCause)).toBe(JSON.stringify(h3Error(fields)))
    expect(JSON.stringify(h3Error({ ...fields, cause: err.cause }))).toBe(JSON.stringify(h3Error(fields)))
  })
})

describe('entryHost', () => {
  it('ENTRY_HOST задан - он без пробелов, wgPublicHost не нужен', () => {
    installStubs({ wgPublicHost: '203.0.113.50' })
    vi.stubEnv('ENTRY_HOST', ' entry.example.test ')
    expect(entryHost()).toBe('entry.example.test')
  })

  it('ENTRY_HOST пуст - wgPublicHost, нет обоих - пустая строка', () => {
    vi.stubEnv('ENTRY_HOST', '')
    installStubs({ wgPublicHost: '203.0.113.50' })
    expect(entryHost()).toBe('203.0.113.50')
    installStubs({})
    expect(entryHost()).toBe('')
  })
})
