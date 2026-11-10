import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { expect, test } from '@playwright/test'
import * as schema from '../server/database/schema'
import { serverModule } from './support/server-module'
import { eq } from 'drizzle-orm'
import * as outbounds from '../shared/outbounds'
import * as plural from '../shared/plural'

test.describe('bot-delivery', () => {
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
    const journal: Array<{ type: string, detail?: string }> = []
    const load = () => serverModule<typeof import('../server/utils/bot-events')>(
      new URL('../server/utils/bot-events.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        './telegram-config': { botConfigured: () => true },
        './events': { recordEvent: async (row: { type: string, detail?: string }) => { journal.push(row); return true } },
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
      expect(journal, 'окончательная ошибка доставки — тревога в журнале').toEqual([
        expect.objectContaining({ type: 'bot_delivery_failed', detail: 'Уведомление клиенту «Тест»' }),
      ])
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
      expect(journal.filter(row => row.type === 'bot_delivery_failed'), 'просроченное — тоже тревога, один раз').toHaveLength(2)

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

  test('бот не настроен: уведомления и рассылка не встают в очередь, накопленное не повторяется и не видно', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'panel-outbox-'))
    const client = createClient({ url: `file:${join(directory, 'panel.sqlite')}` })
    const db = drizzle(client, { schema })
    let configured = false
    let posts = 0
    const telegram = { botConfigured: () => configured }
    const globals = {
      // Без telegram: в config.yaml деплой не выдаёт панели секрет бота.
      useRuntimeConfig: () => ({ tgbotSecret: configured ? 'test' : '' }),
      createError: (error: object) => Object.assign(new Error('bot'), error),
      $fetch: async (_url: string, options: { body: { deliveryId: string } }) => {
        posts++
        return { ok: true, delivery_id: options.body.deliveryId }
      },
    }
    const events = serverModule<typeof import('../server/utils/bot-events')>(
      new URL('../server/utils/bot-events.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        './telegram-config': telegram,
        './events': { recordEvent: async () => true },
      }, globals,
    )
    const broadcast = serverModule<{ default: (event: unknown) => Promise<unknown> }>(
      new URL('../server/api/admin/broadcast.post.ts', import.meta.url), {
        '../../database/client': { useDb: () => ({ select: () => ({ from: () => ({ where: async () => [{ chatId: 100 }] }) }) }) },
        '../../database/schema': schema,
        '../../utils/auth': { requireAuth: async () => {} },
        '../../utils/bot-events': events,
        '../../utils/telegram-config': telegram,
        '../../utils/validate': { readBodyAs: async () => ({ text: 'объявление' }) },
      }, { ...globals, defineEventHandler: (fn: unknown) => fn },
    )
    const outbox = async () => (await db.select({ status: schema.botOutbox.status, attempts: schema.botOutbox.attempts }).from(schema.botOutbox))
    try {
      await migrate(db, { migrationsFolder: 'server/database/migrations' })
      // Так выглядит уже поставленная панель без бота: семь «повторов» в очереди.
      const now = Date.now()
      await db.insert(schema.botOutbox).values(Array.from({ length: 7 }, (_, i) => ({
        id: `stale-${i}`, label: `Уведомление о клиенте «c${i}»`, payload: '{}', status: 'queued' as const,
        attempts: 3, due: now, expires: now + 3_600_000, created: now, lastFailure: now,
      })))

      const shown = await events.botDeliveries()
      const created = await events.notifyBot('client_created', { name: 'Новый' })
      await events.retryBotEvents()
      const left = await outbox()
      const sent = await broadcast.default({}).catch((error: { statusCode?: number, message?: string }) => ({ statusCode: error.statusCode, message: error.message }))
      console.log('без бота:', JSON.stringify({ shown: shown.length, created, outbox: left, broadcast: sent }))
      expect(shown, 'накопленное не показывать очередью').toEqual([])
      expect(created, 'новое уведомление не ставить').toBeNull()
      expect(left, 'накопленное не повторять').toEqual([])
      expect(sent, 'рассылка без бота: понятный отказ, а не молчаливый успех').toEqual({ statusCode: 409, message: 'Telegram-бот не настроен. Рассылка не отправлена.' })
      expect(posts).toBe(0)

      // Бот настроен: прежнее поведение.
      configured = true
      expect(await events.notifyBot('client_created', { name: 'Новый' })).toMatchObject({ status: 'accepted' })
      expect(await broadcast.default({})).toMatchObject({ recipients: 1, delivery: { label: 'Рассылка', status: 'accepted' } })
      expect(posts).toBe(2)
    }
    finally {
      client.close()
      rmSync(directory, { recursive: true, force: true })
    }
  })
})

test.describe('expiry-reminder', () => {
  const DAY = 86_400_000

  test('срок истекает: одна запись и одно напоминание на срок, переживает рестарт панели', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'panel-expiry-'))
    const url = `file:${join(directory, 'panel.sqlite')}`
    let client = createClient({ url })
    let db = drizzle(client, { schema })
    const sent: Array<{ chatId: number, text: string, name: string }> = []

    const logger = { './logger': { useLogger: () => ({ error: () => {}, warn: () => {} }) } }
    const load = () => {
      const journal = serverModule<typeof import('../server/utils/events')>(new URL('../server/utils/events.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        '../../shared/outbounds': outbounds,
        ...logger,
      })
      const reminder = serverModule<typeof import('../server/utils/expiry-reminder')>(new URL('../server/utils/expiry-reminder.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        '../../shared/plural': plural,
        './bot-events': { notifyClient: async (chatId: number, text: string, name: string) => { sent.push({ chatId, text, name }); return null } },
        './events': journal,
        './telegram-config': { botConfigured: () => true },
        ...logger,
      })
      return { journal, reminder }
    }
    try {
      await migrate(db, { migrationsFolder: 'server/database/migrations' })
      // Полночь по Москве: дата в тексте однозначна.
      const now = Date.UTC(2031, 4, 17, 21, 0, 0)
      const at = (ms: number) => new Date(now + ms)
      await db.insert(schema.clients).values([
        { name: 'Скоро', password: 'a'.repeat(20), tgChatId: 501, expiresAt: at(3 * DAY) },
        { name: 'Без телеграма', password: 'b'.repeat(20), expiresAt: at(DAY + 60_000) },
        { name: 'Рано', password: 'c'.repeat(20), tgChatId: 503, expiresAt: at(3 * DAY + 60_000) },
        { name: 'Истёк', password: 'd'.repeat(20), tgChatId: 504, expiresAt: at(-60_000) },
        { name: 'Заморожен', password: 'e'.repeat(20), tgChatId: 505, frozenManual: true, expiresAt: at(DAY) },
        { name: 'Бессрочный', password: 'f'.repeat(20), tgChatId: 506 },
      ])

      let { journal, reminder } = load()
      await reminder.remindExpiring(now)
      await reminder.remindExpiring(now + 30_000)
      expect(sent).toEqual([{ chatId: 501, name: 'Скоро', text: '⏳ Доступ к VPN закончится через 3 дня — 21.05.2031. Чтобы продлить, напишите администратору.' }])
      const rows = () => journal.recentEvents(100).then(r => r.map(e => [e.type, e.level, e.title, e.detail]).sort())
      expect(await rows()).toEqual([
        ['client_expiring', 'warn', 'Доступ истекает · Без телеграма', 'через 2 дня'],
        ['client_expiring', 'warn', 'Доступ истекает · Скоро', 'через 3 дня · напоминание ушло в Telegram'],
      ])

      // Рестарт панели: состояние только в базе — повтора нет.
      client.close()
      client = createClient({ url })
      db = drizzle(client, { schema })
      ;({ journal, reminder } = load())
      await reminder.remindExpiring(now + 50_000)
      expect(sent).toHaveLength(1)
      expect(await journal.recentEvents(100)).toHaveLength(2)

      // «Рано» вошёл в окно — его напоминание; продление «Скоро» и снова 3 дня — новое.
      await db.update(schema.clients).set({ expiresAt: at(5 * DAY) }).where(eq(schema.clients.name, 'Скоро'))
      await reminder.remindExpiring(now + 2 * DAY + 120_000)
      expect(sent.map(s => s.name).sort()).toEqual(['Рано', 'Скоро', 'Скоро'])
      expect(sent.find((s, i) => i > 0 && s.name === 'Скоро')!.text).toContain('через 3 дня — 23.05.2031')
    }
    finally {
      client.close()
      rmSync(directory, { recursive: true, force: true })
    }
  })
})
