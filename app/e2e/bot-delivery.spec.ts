import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { test, expect } from '@playwright/test'
import * as schema from '../server/database/schema'
import { serverModule } from './support/server-module'

test('бот лежит: постоянная очередь, повторы, рестарт, подтверждение и окончательная ошибка', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'panel-outbox-'))
  const url = `file:${join(directory, 'panel.sqlite')}`
  let client = createClient({ url })
  let db = drizzle(client, { schema })
  let now = Date.now()
  let available = false
  let timeoutAfterAccept = false
  let attempts = 0
  let whileSending: unknown
  const delivered = new Set<string>()
  const load = () => serverModule<typeof import('../server/utils/bot-events')>(
    new URL('../server/utils/bot-events.ts', import.meta.url), {
      '../database/client': { useDb: () => db },
      '../database/schema': schema,
    }, {
      Date: { now: () => now },
      useRuntimeConfig: () => ({ tgbotSecret: 'test' }),
      createError: (error: object) => Object.assign(new Error('queue'), error),
      $fetch: async (_url: string, options: { body: { deliveryId: string } }) => {
        attempts++
        if (attempts === 1) whileSending = await load().botDeliveries()
        if (!available) throw new Error('ECONNREFUSED')
        delivered.add(options.body.deliveryId)
        if (timeoutAfterAccept) throw new Error('timeout after acceptance')
        return { ok: true, delivery_id: options.body.deliveryId }
      },
    },
  )
  try {
    await migrate(db, { migrationsFolder: 'server/database/migrations' })
    let queue = load()
    const first = await queue.notifyBot('client_notify', { chatId: 100, text: 'hello' }, 'Уведомление клиенту «Тест»')
    expect(first, 'не выдавать молчаливый успех при недоступном боте').toMatchObject({ status: 'queued', remaining: 5 })
    expect(attempts).toBe(1)
    expect(whileSending, 'не показывать ошибку до завершения первой попытки').toEqual([])
    expect(await queue.botDeliveries()).toEqual([first])
    // Закрываем соединение и пересоздаём модуль: состояние только в файле SQLite.
    client.close()
    client = createClient({ url })
    db = drizzle(client, { schema })
    queue = load()
    available = true
    now += 30_000
    await queue.retryBotEvents()
    expect(delivered.size).toBe(1)
    expect((await queue.botDeliveries())[0]?.status).toBe('accepted')
    await queue.retryBotEvents()
    expect(attempts).toBe(2)

    timeoutAfterAccept = true
    const second = await queue.notifyBot('client_notify', { chatId: 100, text: 'uncertain' }, 'Уведомление клиенту «Тест»')
    expect(second.status).toBe('queued')
    now += 30_000
    timeoutAfterAccept = false
    await load().retryBotEvents()
    expect(delivered.size).toBe(2)

    available = false
    const last = await queue.notifyBot('client_notify', { chatId: 100, text: 'fail' }, 'Уведомление клиенту «Тест»')
    for (const delay of [30, 60, 120, 240, 480]) {
      now += delay * 1000
      await load().retryBotEvents()
    }
    expect((await queue.botDeliveries()).find(row => row.id === last.id)).toMatchObject({ status: 'failed', remaining: 0 })
    const exhausted = attempts
    now += 60_000
    await queue.retryBotEvents()
    expect(attempts).toBe(exhausted)

    const expired = await queue.notifyBot('client_notify', { chatId: 100, text: 'expired' }, 'Уведомление клиенту «Тест»')
    now += 60 * 60 * 1000
    const beforeExpiry = attempts
    await load().retryBotEvents()
    expect(attempts).toBe(beforeExpiry)
    expect((await queue.botDeliveries()).find(row => row.id === expired.id)?.status).toBe('failed')

    const stored = await db.select().from(schema.botOutbox)
    expect(stored.every(row => row.payload === '')).toBe(true)
    now += 24 * 60 * 60 * 1000
    await queue.retryBotEvents()
    expect(await queue.botDeliveries()).toEqual([])

    await db.insert(schema.botOutbox).values(Array.from({ length: 1024 }, (_, i) => ({
      id: `full-${i}`, label: 'fixture', payload: '{}', status: 'queued' as const,
      due: now + 30_000, expires: now + 3_600_000, created: now, lastFailure: now,
    })))
    const overflow = await queue.notifyBot('client_notify', { chatId: 100, text: 'overflow' }, 'Уведомление клиенту «Тест»').catch(error => error)
    expect(overflow).toMatchObject({ statusCode: 503 })
    expect(await queue.botDeliveries()).toHaveLength(1024)
  }
  finally {
    client.close()
    rmSync(directory, { recursive: true, force: true })
  }
})
