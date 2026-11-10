import { beforeEach, describe, expect, it, vi } from 'vitest'
import { auth, calls, dbResults, dbRows, fakeEvent, installStubs, rejection, stubError } from '../test/stubs'
import { adminClientDevice, botClientDevice } from './device-route'

vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./bot-api', async () => (await import('../test/stubs')).botApiModule)

const client = { id: 7, name: 'Анна' }
const device = { id: 3, clientId: 7, name: 'phone' }
const preflightError = stubError({ statusCode: 503, statusMessage: 'bot_not_configured', message: 'Telegram-бот не настроен' })
const failingPreflight = () => {
  calls.push('preflight')
  throw preflightError
}
const preflight = () => {
  calls.push('preflight')
  return 'endpoint'
}

beforeEach(() => installStubs())

const cases = [
  { name: 'adminClientDevice', run: adminClientDevice, ok: fakeEvent({ id: '7', deviceId: '3' }), bad: fakeEvent({ id: 'x', deviceId: '3' }), invalid: 'invalid_id', authFail: auth.admin, lookups: ['db:select', 'db:select'] },
  { name: 'botClientDevice', run: botClientDevice, ok: fakeEvent({ deviceId: '3' }, { chatId: '42' }), bad: fakeEvent({ deviceId: '3' }, {}), invalid: 'invalid_params', authFail: auth.bot, lookups: ['db:clientByChat', 'db:select'] },
] as const

describe.each(cases)('$name', ({ run, ok, bad, invalid, authFail, lookups }) => {
  it('отказ авторизации - до разбора параметров и проверок', async () => {
    authFail.mockImplementation(() => { throw stubError({ statusCode: 401, statusMessage: 'unauthorized' }) })
    const err = await rejection(run(bad, failingPreflight))
    expect(err.statusCode).toBe(401)
    expect(calls).toEqual(['auth'])
  })

  it(`плохие параметры - 400 ${invalid} до preflight`, async () => {
    const err = await rejection(run(bad, failingPreflight))
    expect([err.statusCode, err.statusMessage]).toEqual([400, invalid])
    expect(calls).toEqual(['auth'])
  })

  it('ошибка preflight - до поиска в базе, даже когда девайса нет', async () => {
    const err = await rejection(run(ok, failingPreflight))
    expect(err).toBe(preflightError)
    expect(calls).toEqual(['auth', 'preflight'])
  })

  it('нет клиента - 404 после preflight', async () => {
    const err = await rejection(run(ok, preflight))
    expect(err.statusCode).toBe(404)
    expect(calls).toEqual(['auth', 'preflight', lookups[0]])
  })

  it('нет девайса - 404 not_found', async () => {
    dbResults.push([client])
    const err = await rejection(run(ok, preflight))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect(calls).toEqual(['auth', 'preflight', ...lookups])
  })

  it('клиент, девайс и результат preflight', async () => {
    dbResults.push([client], [device])
    const found = await run(ok, preflight)
    expect(found).toMatchObject({ client, device, ready: 'endpoint' })
    expect(calls).toEqual(['auth', 'preflight', ...lookups])
  })
})

it('админ без клиента - 404 not_found', async () => {
  const err = await rejection(adminClientDevice(fakeEvent({ id: '7', deviceId: '3' })))
  expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
})

it('бот без привязки чата - 404 not_linked, chatId в ответе', async () => {
  expect((await rejection(botClientDevice(fakeEvent({ deviceId: '3' }, { chatId: '42' })))).statusMessage).toBe('not_linked')
  dbResults.push([client], [device])
  expect((await botClientDevice(fakeEvent({ deviceId: '3' }, { chatId: '42' }))).chatId).toBe(42)
})

describe('принадлежность девайса клиенту', () => {
  const rows = () => {
    dbRows.clients = [{ id: 7, name: 'Анна', tgChatId: 42 }, { id: 8, name: 'Борис', tgChatId: 43 }]
    dbRows.devices = [{ id: 3, clientId: 7, name: 'phone' }, { id: 4, clientId: 8, name: 'laptop' }]
  }

  it('админ: девайс чужого клиента - 404, свой находится', async () => {
    rows()
    const err = await rejection(adminClientDevice(fakeEvent({ id: '7', deviceId: '4' })))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect((await adminClientDevice(fakeEvent({ id: '7', deviceId: '3' }))).device).toMatchObject({ id: 3, clientId: 7 })
  })

  it('бот: девайс клиента другого чата - 404, свой находится', async () => {
    rows()
    const err = await rejection(botClientDevice(fakeEvent({ deviceId: '4' }, { chatId: '42' })))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect((await botClientDevice(fakeEvent({ deviceId: '4' }, { chatId: '43' }))).device).toMatchObject({ id: 4, clientId: 8 })
  })

  it('id не целое, но конечное (1.5) - не 400: проверка Number.isFinite, как в маршрутах', async () => {
    rows()
    const admin = await rejection(adminClientDevice(fakeEvent({ id: '1.5', deviceId: '3' })))
    expect([admin.statusCode, admin.statusMessage]).toEqual([404, 'not_found'])
    const bot = await rejection(botClientDevice(fakeEvent({ deviceId: '1.5' }, { chatId: '42' })))
    expect([bot.statusCode, bot.statusMessage]).toEqual([404, 'not_found'])
  })
})
