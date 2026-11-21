import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import * as schema from '../database/schema'
import { pruneEvents } from './bot'
import { minExpiryMs, reportControlExpiry } from './expiry'

// Журнал хоста: всё, что служба когда-либо сняла по сроку (хранится без срока).
const host = vi.hoisted(() => ({ removed: [] as { protocol: string, id: number, expires_at: string, at: string }[], db: null as unknown }))
vi.mock('../database/client', () => ({ useDb: () => host.db }))
vi.mock('./control', () => ({ control: async () => ({ removed: host.removed }) }))
vi.stubGlobal('useLogger', () => ({ info: () => {}, warn: () => {}, error: () => {} }))
vi.stubGlobal('useRuntimeConfig', () => ({ ovpnEnabled: true }))


beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

const at = (iso: string) => vi.setSystemTime(new Date(iso))

describe('minExpiryMs', () => {
  it('начало завтрашнего дня по UTC: посреди дня', () => {
    at('2026-06-15T12:34:56.789Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00.000Z'))
  })

  it('ровно полночь UTC - всё равно следующая полночь', () => {
    at('2026-06-15T00:00:00.000Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00.000Z'))
  })

  it('последняя миллисекунда дня - полночь через 1 мс', () => {
    at('2026-06-15T23:59:59.999Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00.000Z'))
  })

  it('переход через границу месяца и года', () => {
    at('2026-01-31T10:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-02-01T00:00:00Z'))
    at('2026-12-31T23:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2027-01-01T00:00:00Z'))
  })

  it('29 февраля високосного и непросекосного года', () => {
    at('2028-02-28T05:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2028-02-29T00:00:00Z'))
    at('2027-02-28T05:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2027-03-01T00:00:00Z'))
  })

  it('зона процесса не влияет: решает UTC, а не локальная дата', () => {
    // 23:30 UTC 15-го - для UTC+3 уже 16-е, завтра по UTC всё равно 16-е
    at('2026-06-15T23:30:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00Z'))
    at('2026-06-16T00:30:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-17T00:00:00Z'))
  })

  it('любая зона процесса: UTC+14, UTC-11 и UTC дают тот же результат', () => {
    const prev = process.env.TZ
    try {
      for (const tz of ['Pacific/Kiritimati', 'Pacific/Pago_Pago', 'UTC']) {
        process.env.TZ = tz
        at('2026-06-15T12:00:00Z')
        expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00Z'))
        at('2026-06-15T23:30:00Z')
        expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00Z'))
        at('2026-06-16T00:30:00Z')
        expect(minExpiryMs()).toBe(Date.parse('2026-06-17T00:00:00Z'))
      }
    }
    finally {
      if (prev === undefined) delete process.env.TZ
      else process.env.TZ = prev
    }
  })

  it('результат всегда больше «сейчас» и в пределах суток', () => {
    for (const iso of ['2026-03-29T00:00:00Z', '2026-10-25T01:59:59Z', '2026-06-15T12:00:00Z']) {
      at(iso)
      const diff = minExpiryMs() - Date.now()
      expect(diff).toBeGreaterThan(0)
      expect(diff).toBeLessThanOrEqual(86_400_000)
    }
  })
})

const NOW = Date.parse('2026-11-21T12:00:00.000Z')
const DAY = 24 * 3600 * 1000
const entry = (id: number, ms: number) => ({ protocol: 'openvpn', id, expires_at: new Date(ms - 1000).toISOString(), at: new Date(ms).toISOString() })

describe('reportControlExpiry', () => {
  let db: ReturnType<typeof drizzle<typeof schema>>
  beforeAll(async () => {
    db = drizzle(createClient({ url: ':memory:' }), { schema })
    await migrate(db, { migrationsFolder: fileURLToPath(new URL('../database/migrations', import.meta.url)) })
    host.db = db
  })
  beforeEach(async () => {
    vi.useRealTimers()
    vi.useFakeTimers({ now: NOW, toFake: ['Date'] })
    await db.delete(schema.events)
  })

  const logged = async () => (await db.select().from(schema.events)).map(e => e.key)

  it('запись 40-дневной давности: два минутных цикла и часовая чистка - в журнале её нет, вставок нет', async () => {
    host.removed = [entry(1, NOW - 40 * DAY)]
    for (let minute = 0; minute < 2; minute++) {
      await reportControlExpiry()
      vi.setSystemTime(NOW + (minute + 1) * 60_000)
    }
    expect(await logged()).toEqual([])
    await pruneEvents()
    await reportControlExpiry()
    expect(await logged()).toEqual([])
  })

  it('свежее снятие записывается ровно один раз, в том числе после перезапуска панели', async () => {
    host.removed = [entry(2, NOW - 60_000)]
    await reportControlExpiry()
    await reportControlExpiry()
    vi.resetModules()
    await (await import('./expiry')).reportControlExpiry()
    expect(await logged()).toEqual([`control-expiry:openvpn:2:${host.removed[0]!.expires_at}`])
  })

  it.each([-1, 0, 1])('граница хранения %i мс: вставляется ровно то, что чистка оставит', async (shift) => {
    const retention = 30 * DAY
    host.removed = [entry(3, NOW - retention + shift)]
    await reportControlExpiry()
    const inserted = (await logged()).length
    await pruneEvents()
    expect((await logged()).length).toBe(inserted)
    await reportControlExpiry()
    expect((await logged()).length).toBe(inserted)
  })

  it('часы хоста впереди панели: снятие «из будущего» записывается один раз', async () => {
    host.removed = [entry(4, NOW + 5 * 60_000)]
    await reportControlExpiry()
    await pruneEvents()
    await reportControlExpiry()
    expect((await logged()).length).toBe(1)
  })
})
