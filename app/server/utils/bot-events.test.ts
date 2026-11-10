import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dbOps, dbResults, fetchMock, installStubs, rejection } from '../test/stubs'
import { botSecretOrThrow, retryBotEvents, sendBotEvent } from './bot-events'

vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./events', () => ({ recordEvent: vi.fn() }))
vi.mock('./telegram-config', async importOriginal => ({
  ...await importOriginal<typeof import('./telegram-config')>(),
  botConfigured: () => true,
}))

const body = { type: 'client_send_password', name: 'Анна', password: 'p' }

beforeEach(() => installStubs({ tgbotSecret: 's3cret', tgbotEventPort: 9911 }))

describe('botSecretOrThrow', () => {
  it('отдаёт секрет', () => {
    expect(botSecretOrThrow()).toBe('s3cret')
  })

  it('без секрета - 503 bot_not_configured', () => {
    installStubs({})
    expect(() => botSecretOrThrow()).toThrow(expect.objectContaining({
      statusCode: 503, statusMessage: 'bot_not_configured', message: 'Telegram-бот не настроен',
    }))
  })
})

describe('sendBotEvent', () => {
  it('POST /event с секретом, таймаут 8000', async () => {
    await sendBotEvent(body)
    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:9911/event', {
      method: 'POST', headers: { 'X-Tgbot-Secret': 's3cret' }, body, timeout: 8000, retry: 0,
    })
  })

  it('порт по умолчанию 8877', async () => {
    installStubs({ tgbotSecret: 's3cret' })
    await sendBotEvent(body)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('http://127.0.0.1:8877/event')
  })

  it('бот не ответил - 502 bot_unreachable с причиной', async () => {
    const cause = new Error('ECONNREFUSED')
    fetchMock.mockRejectedValue(cause)
    const err = await rejection(sendBotEvent(body))
    expect(err).toMatchObject({ statusCode: 502, statusMessage: 'bot_unreachable', message: 'Бот не отвечает — проверь /api/admin/telegram' })
    expect(err.cause).toBe(cause)
  })
})

describe('очередь outbox', () => {
  const queued = () => {
    const now = Date.now()
    // expired, rows к доставке, кандидат в аренде
    dbResults.push([], [], [{ id: 'd1' }], [{ id: 'd1', label: 'L', status: 'queued', attempts: 0, due: now, expires: now + 60_000, payload: JSON.stringify(body) }])
  }

  it('шлёт с deliveryId, таймаут 1500 и retry 0', async () => {
    queued()
    fetchMock.mockResolvedValue({ ok: true, delivery_id: 'd1' })
    await retryBotEvents()
    expect(fetchMock).toHaveBeenCalledWith('http://127.0.0.1:9911/event', {
      method: 'POST', headers: { 'X-Tgbot-Secret': 's3cret' }, body: { ...body, deliveryId: 'd1' }, timeout: 1500, retry: 0,
    })
    expect(dbOps.some(o => o.op === 'set' && (o.args[0] as { status?: string }).status === 'accepted')).toBe(true)
  })

  it('отказ бота - запись остаётся в очереди, без ошибки наружу', async () => {
    queued()
    fetchMock.mockRejectedValue(new Error('timeout'))
    await retryBotEvents()
    const last = dbOps.filter(o => o.op === 'set').at(-1)?.args[0] as { status?: string, lastFailure?: number }
    expect(last.status).toBe('queued')
    expect(last.lastFailure).toBeGreaterThan(0)
  })
})
