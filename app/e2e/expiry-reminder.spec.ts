import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createClient } from '@libsql/client'
import { eq } from 'drizzle-orm'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { test, expect } from '@playwright/test'
import * as schema from '../server/database/schema'
import * as outbounds from '../shared/outbounds'
import * as plural from '../shared/plural'
import { serverModule } from './support/server-module'

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
