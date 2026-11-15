import { beforeEach, describe, expect, it, vi } from 'vitest'
import { installStubs } from './stubs'

const recordEvent = vi.hoisted(() => vi.fn())
const notifyBackupFailed = vi.hoisted(() => vi.fn())
const deliveryRejected = vi.hoisted(() => vi.fn())
const body = vi.hoisted(() => ({ value: {} as Record<string, unknown> }))

vi.mock('../utils/bot-api', async () => (await import('./stubs')).botApiModule)
vi.mock('../utils/bot', async importOriginal => ({ ...await importOriginal<object>(), recordEvent, notifyBackupFailed, deliveryRejected }))
vi.mock('../utils/api', async importOriginal => ({
  ...await importOriginal<object>(),
  readBodyAs: async (_event: unknown, schema: { parse: (v: unknown) => unknown }) => schema.parse(body.value),
}))
vi.stubGlobal('defineEventHandler', (handler: unknown) => handler)

const handler = (await import('../resources/settings')).default['POST /bot/events'] as unknown as (event: unknown) => Promise<unknown>

beforeEach(() => {
  installStubs()
  recordEvent.mockReset()
  notifyBackupFailed.mockReset()
  deliveryRejected.mockReset()
})

describe('POST /api/bot/events', () => {
  it('отказ бэкапа - строка ленты и сообщение боту', async () => {
    body.value = { type: 'backup_failed', detail: 'нет пароля шифрования' }
    await expect(handler({})).resolves.toEqual({ ok: true })
    expect(recordEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'backup_failed', detail: 'нет пароля шифрования' }))
    expect(notifyBackupFailed).toHaveBeenCalledExactlyOnceWith('нет пароля шифрования')
  })

  it('предупреждение и отказ Telegram боту не пересылаются', async () => {
    for (const value of [{ type: 'backup_warning', detail: 'x' }, { type: 'telegram_rejected', status: 403, recipient: 'admin' }]) {
      body.value = value
      await handler({})
    }
    expect(recordEvent).toHaveBeenCalledTimes(2)
    expect(notifyBackupFailed).not.toHaveBeenCalled()
    expect(deliveryRejected, 'отказ без id доставки (бот до обновления) - только строка ленты').not.toHaveBeenCalled()
  })

  it('отказ Telegram с id доставки отмечает её, лента называет уведомление', async () => {
    deliveryRejected.mockResolvedValue('Рассылка')
    body.value = { type: 'telegram_rejected', status: 400, recipient: 'client', deliveryId: 'd-1' }
    await expect(handler({})).resolves.toEqual({ ok: true })
    expect(deliveryRejected).toHaveBeenCalledExactlyOnceWith('d-1', 400)
    expect(recordEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'telegram_rejected', detail: 'Рассылка · клиенту · код 400' }))
  })

  it('смена маршрута YouTube сторожем - строка ленты с тем же текстом, что боту', async () => {
    for (const [route, detail] of [['foreign-best', 'youtube-ru → foreign-best; 4 МиБ за 30 с'], ['youtube-ru', 'foreign-best → youtube-ru; 4 МиБ за 2 с']]) {
      body.value = { type: 'youtube_route', route, detail }
      await expect(handler({})).resolves.toEqual({ ok: true })
    }
    expect(recordEvent.mock.calls.map(([e]) => [e.level, e.title, e.detail])).toEqual([
      ['warn', 'Маршрут YouTube', 'youtube-ru → foreign-best; 4 МиБ за 30 с'],
      ['ok', 'Маршрут YouTube', 'foreign-best → youtube-ru; 4 МиБ за 2 с'],
    ])
    body.value = { type: 'youtube_route', route: 'other', detail: 'x' }
    await expect(handler({})).rejects.toThrow()
    expect(notifyBackupFailed).not.toHaveBeenCalled()
  })
})
