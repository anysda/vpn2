import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dbOps, dbResults, fetchMock, installStubs, rejection } from '../test/stubs'
import { botSecretOrThrow, notifyBackupFailed, retryBotEvents, sendBotEvent } from './bot-events'

vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./events', () => ({ recordEvent: vi.fn() }))
vi.mock('./telegram-config', async importOriginal => ({
  ...await importOriginal<typeof import('./telegram-config')>(),
  botConfigured: () => configured.value,
}))

const configured = vi.hoisted(() => ({ value: true }))

const body = { type: 'client_send_password', name: 'Анна', password: 'p' }

beforeEach(() => {
  configured.value = true
  installStubs({ tgbotSecret: 's3cret', tgbotEventPort: 9911 })
})

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

  it('без секрета - запрос боту не уходит, запись остаётся в очереди', async () => {
    installStubs({ tgbotEventPort: 9911 })
    queued()
    await retryBotEvents()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(dbOps.filter(o => o.op === 'set').at(-1)?.args[0]).toMatchObject({ status: 'queued' })
  })

  it('подтверждение без delivery_id или с чужим - не доставка', async () => {
    for (const ack of [{ ok: true }, { ok: true, delivery_id: 'other' }]) {
      installStubs({ tgbotSecret: 's3cret', tgbotEventPort: 9911 })
      queued()
      fetchMock.mockResolvedValue(ack)
      await retryBotEvents()
      expect(dbOps.filter(o => o.op === 'set').at(-1)?.args[0]).toMatchObject({ status: 'queued' })
    }
  })
})

describe('отказ бэкапа', () => {
  const inserted = () => dbOps.filter(o => o.op === 'values').map(o => o.args[0] as { label: string, payload: string, status: string })

  it('ставит в очередь system_alert и сразу шлёт боту', async () => {
    // нет ожидающего, чистка, размер очереди, вставка, кандидат в аренде
    const now = Date.now()
    const text = JSON.stringify({ type: 'system_alert', subject: 'Бэкап не удался', detail: 'нет пароля шифрования' })
    dbResults.push([], [], [{ n: 0 }], [], [{ id: 'x', label: 'L', status: 'queued', attempts: 0, due: now, expires: now + 60_000, payload: text }])
    fetchMock.mockImplementation(async (_url, opts) => ({ ok: true, delivery_id: (opts.body as { deliveryId: string }).deliveryId }))
    await notifyBackupFailed('нет пароля шифрования')
    const [row] = inserted()
    expect(row).toMatchObject({ label: 'Сообщение об отказе бэкапа', status: 'queued' })
    expect(JSON.parse(row!.payload)).toEqual({ type: 'system_alert', subject: 'Бэкап не удался', detail: 'нет пароля шифрования' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1].body).toMatchObject({ type: 'system_alert', subject: 'Бэкап не удался', detail: 'нет пароля шифрования' })
  })

  it('второй отказ при недоставленном первом новой записи не добавляет', async () => {
    dbResults.push([{ id: 'd1', label: 'Сообщение об отказе бэкапа', status: 'queued', attempts: 1, created: 1 }])
    await notifyBackupFailed('архив не выгружен')
    expect(inserted()).toHaveLength(0)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('отказ бэкапа без бота', () => {
  it('бот не настроен - ни записи, ни запроса, ни ошибки', async () => {
    configured.value = false
    await expect(notifyBackupFailed('нет места')).resolves.toBeUndefined()
    expect(dbOps).toHaveLength(0)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('очередь заполнена - ошибка наружу не выходит', async () => {
    dbResults.push([], [], [{ n: 1024 }], [])
    await expect(notifyBackupFailed('нет места')).resolves.toBeUndefined()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
