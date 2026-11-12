import { beforeEach, describe, expect, it, vi } from 'vitest'
import { installStubs } from './stubs'

const recordEvent = vi.hoisted(() => vi.fn())
const notifyBackupFailed = vi.hoisted(() => vi.fn())
const body = vi.hoisted(() => ({ value: {} as Record<string, unknown> }))

vi.mock('../utils/bot-api', async () => (await import('./stubs')).botApiModule)
vi.mock('../utils/bot', async importOriginal => ({ ...await importOriginal<object>(), recordEvent, notifyBackupFailed }))
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
  })
})
