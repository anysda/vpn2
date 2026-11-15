import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { readBodyAs } from './api'

// Ошибка поля называет его так же, как форма, и говорит, чего поле ждёт.
vi.stubGlobal('readBody', async (event: { body: unknown }) => event.body)
vi.stubGlobal('createError', (fields: { message?: string }) => Object.assign(new Error(fields.message), fields))
vi.stubGlobal('isError', () => false)

const fail = (schema: z.ZodType, body: unknown) =>
  readBodyAs({ body } as never, schema).then(() => '', (e: Error) => e.message)

describe('readBodyAs: текст ошибки поля', () => {
  it('поля бота - подписями формы, Chat ID - с подсказкой', async () => {
    const bot = z.object({ bot_token: z.string().optional(), chat_id: z.union([z.string().regex(/^-?\d+$/), z.number()]).optional(), admin_username: z.string().optional() })
    expect(await fail(bot, { chat_id: 'abc' })).toBe('Проверьте поле «Chat ID» - только цифры')
    expect(await fail(bot, { bot_token: 1 })).toBe('Проверьте поле «Token»')
    expect(await fail(bot, { admin_username: 1 })).toBe('Проверьте поле «Никнейм админа бота»')
  })
  it('длинная строка - с пределом', async () => {
    expect(await fail(z.object({ name: z.string().max(64) }), { name: 'x'.repeat(70) })).toBe('Проверьте поле «Имя» - до 64 символов')
    expect(await fail(z.object({ name: z.string().min(1) }), { name: '' })).toBe('Проверьте поле «Имя»')
  })
})
