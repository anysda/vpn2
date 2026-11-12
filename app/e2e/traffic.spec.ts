import { expect, test } from '@playwright/test'
import { serverModule } from './support/server-module'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import * as schema from '../server/database/schema'
import { coalesceRuns } from '../server/utils/api'

const mskDay = serverModule<typeof import('../server/utils/metrics')>('server/utils/metrics.ts', {})

test.describe('daily-traffic', () => {
  const instance = '192.0.2.1:9100'
  const midnight = Date.parse('2026-10-12T21:00:00Z')

  function dailyMetrics(start: number) {
    let now = start
    let missing = false
    const queries: Array<{ query: string, time?: number }> = []
    const metrics = serverModule<typeof import('../server/utils/metrics')>(
      new URL('../server/utils/metrics.ts', import.meta.url), {}, {
        useLogger: () => ({ warn: () => {} }),
        Date: { now: () => now },
        useRuntimeConfig: () => ({ vmUrl: 'http://metrics.invalid' }),
        $fetch: async (_url: string, options: { query: { query: string, time?: number } }) => {
          queries.push(options.query)
          return { status: 'success', data: { result: missing ? [] : [{ metric: { instance }, value: [now / 1000, '5000'] }] } }
        },
      },
    )
    return { metrics, queries, setTime: (value: number) => { now = value }, loseMetrics: () => { missing = true } }
  }

  for (const elapsed of [1, 10, 59, 60, 3600]) {
    test(`суточный трафик: окно через ${elapsed} с после полуночи МСК не захватывает вчера`, async () => {
      const { metrics, queries } = dailyMetrics(midnight + elapsed * 1000)
      await metrics.fetchDailyTraffic([instance])
      expect(queries).toHaveLength(2)
      for (const query of queries) {
        expect(query.query).toContain(`[${elapsed}s]`)
        // VictoriaMetrics increase учитывает сэмпл перед окном; он может быть вчерашним.
        expect(query.query).toContain('increase_prometheus(')
        expect(query.time).toBe(midnight / 1000 + elapsed)
      }
    })
  }

  test('ровно в полночь трафик нулевой без запроса за вчера и без кэша на 30 секунд', async () => {
    const { metrics, queries, setTime } = dailyMetrics(midnight)
    expect((await metrics.fetchDailyTraffic([instance])).get(instance)).toEqual({ rxBytes: 0, txBytes: 0 })
    expect(queries).toHaveLength(0)
    setTime(midnight + 2000)
    await metrics.fetchDailyTraffic([instance])
    expect(queries).toHaveLength(2)
  })

  test('полночь очищает вчерашний кэш и holdover даже при отсутствии новых метрик', async () => {
    const { metrics, setTime, loseMetrics } = dailyMetrics(midnight - 1000)
    expect((await metrics.fetchDailyTraffic([instance])).get(instance)?.rxBytes).toBe(5000)
    setTime(midnight)
    expect((await metrics.fetchDailyTraffic([instance])).get(instance)?.rxBytes).toBe(0)
    setTime(midnight + 1000)
    loseMetrics()
    expect((await metrics.fetchDailyTraffic([instance])).get(instance)).toEqual({ rxBytes: null, txBytes: null })
  })
})

test.describe('overview-traffic', () => {
  const NL = '192.0.2.1:9100', FI = '192.0.2.2:9100', RU = '192.0.2.9:9100'

  interface Call { promql: string, start?: number, end?: number, step?: number, time?: number }
  type Rows = Map<string, Array<number | null>>

  // Настоящий exitTraffic с подменой VM: range - ответ query_range,
  // instant - ответы мгновенных запросов по порядку (текущее ведро, затем прошлое окно).
  function load(range: Rows, instant: Array<Map<string, number>>, exits = [{ tag: 'ru', instance: RU }, { tag: 'nl', instance: NL }, { tag: 'fi', instance: FI }]) {
    const calls: { range: Call[], instant: Call[] } = { range: [], instant: [] }
    type Query = { query: string, start: number, end: number, step: string, time?: number }
    const vm = async (url: string, { query }: { query: Query }) => {
      if (url.endsWith('/query_range')) {
        const step = Number.parseInt(query.step)
        calls.range.push({ promql: query.query, start: query.start, end: query.end, step })
        const result = [...range].map(([instance, values]) => ({
          metric: { instance }, values: values.flatMap((v, i) => v === null ? [] : [[query.start + i * step, String(v)]]),
        }))
        return { status: 'success', data: { result } }
      }
      calls.instant.push({ promql: query.query, time: query.time })
      const values = instant[calls.instant.length - 1] ?? new Map()
      return { status: 'success', data: { result: [...values].map(([instance, v]) => ({ metric: { instance }, value: [0, String(v)] })) } }
    }
    const mod = serverModule<typeof import('../server/utils/metrics')>(
      new URL('../server/utils/metrics.ts', import.meta.url), {}, {
        useLogger: () => ({ warn: () => {} }),
        useRuntimeConfig: () => ({
          vmUrl: 'http://metrics.invalid',
          mgmtIps: exits.map(x => `${x.tag}:${x.instance.split(':')[0]}`).join(','),
          exitTags: exits.filter(x => x.tag !== 'ru').map(x => x.tag).join(' '),
        }),
        $fetch: vm,
      },
    )
    return { exitTraffic: mod.exitTraffic, calls }
  }

  // 13 окт 10:30 UTC = 13:30 МСК
  const NOW = Date.parse('2026-10-13T10:30:00Z')
  const HOUR = Date.parse('2026-10-13T10:00:00Z') / 1000

  test('трафик за сутки: 24 часовых ведра по экзитам, текущий час — неполный', async () => {
    const done = Array.from({ length: 23 }, (_, i) => (i + 1) * 1e6) as Array<number | null>
    done[5] = null
    const { exitTraffic, calls } = load(new Map([[NL, done]]), [new Map([[NL, 100.4], [FI, 50]]), new Map([[NL, 7000], [FI, 3000]])])
    const r = await exitTraffic('day', NOW)

    const first = HOUR - 23 * 3600
    expect(r.step).toBe(3600)
    expect(r.starts).toHaveLength(24)
    expect(r.starts[0]).toBe(first)
    expect(r.starts[23]).toBe(HOUR)
    // РФ-вход — не экзит
    expect(r.exits.map(x => x.tag)).toEqual(['nl', 'fi'])

    // полные часы — одним query_range: точка в конце часа считает прирост за него
    expect(calls.range).toEqual([expect.objectContaining({ start: first + 3600, end: HOUR, step: 3600 })])
    expect(calls.range[0]!.promql).toContain('increase_prometheus(')
    expect(calls.range[0]!.promql).toContain('[3600s]')
    // текущий час — от его начала до сейчас; прошлое окно — той же длины сутками раньше
    expect(calls.instant[0]).toEqual({ promql: expect.stringContaining('[1800s]'), time: NOW / 1000 })
    expect(calls.instant[1]).toEqual({ promql: expect.stringContaining(`[${23 * 3600 + 1800}s]`), time: NOW / 1000 - 24 * 3600 })

    const nl = r.exits[0]!.bytes, fi = r.exits[1]!.bytes
    expect(nl[0]).toBe(1e6)
    expect(nl[5]).toBeNull()
    expect(nl[23]).toBe(100)
    expect(fi.slice(0, 23).every(v => v === null)).toBe(true)
    expect(fi[23]).toBe(50)
    // пропуски не рвут сумму: считаются нулём
    expect(r.total[5]).toBe(0)
    expect(r.total[0]).toBe(1e6)
    expect(r.total[23]).toBe(150)
    expect(r.sum).toBe(r.total.reduce((a, b) => a + b, 0))
    expect(r.prev).toBe(10000)
  })

  test('трафик за сутки ровно в начале часа: текущее ведро нулевое, без запроса', async () => {
    const { exitTraffic, calls } = load(new Map(), [], [{ tag: 'nl', instance: NL }])
    const r = await exitTraffic('day', HOUR * 1000)
    expect(calls.instant).toHaveLength(1)
    expect(r.total[23]).toBe(0)
    expect(r.total.every(v => v === 0)).toBe(true)
    // VM молчит за прошлое окно — сравнения нет
    expect(r.prev).toBeNull()
  })

  test('трафик за неделю: 7 суток МСК, последние — с полуночи МСК до сейчас', async () => {
    const { exitTraffic, calls } = load(new Map([[NL, [1, 2, 3, 4, 5, 6]]]), [new Map([[NL, 7]])], [{ tag: 'nl', instance: NL }])
    const r = await exitTraffic('week', NOW)
    const midnight = Date.parse('2026-10-12T21:00:00Z') / 1000
    expect(r.step).toBe(86400)
    expect(r.starts).toHaveLength(7)
    expect(r.starts[6]).toBe(midnight)
    expect(calls.range[0]).toEqual(expect.objectContaining({ start: midnight - 5 * 86400, end: midnight, step: 86400 }))
    expect(calls.range[0]!.promql).toContain('[86400s]')
    expect(calls.instant[0]!.promql).toContain(`[${NOW / 1000 - midnight}s]`)
    expect(r.total).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(r.sum).toBe(28)
  })

  test('активность по суткам: онлайн — клиенты от порога за сутки, трафик — сумма всех', async () => {
    const MIN = 1024 ** 2
    // запрос уже сгруппирован базой по суткам МСК и клиенту — проверяем свёртку
    const rows = [
      { day: 20741, clientId: 1, bytes: MIN },
      { day: 20740, clientId: 1, bytes: MIN - 1 },
      { day: 20740, clientId: 2, bytes: String(5 * MIN) },
      { day: 20741, clientId: 2, bytes: 3 },
    ]
    const query = { select: () => query, from: () => query, innerJoin: () => query, groupBy: async () => rows }
    const { activityDays } = serverModule<typeof import('../server/utils/traffic')>(
      new URL('../server/utils/traffic.ts', import.meta.url), {
        'drizzle-orm': { eq: () => null, sql: () => null },
        '../database/client': { useDb: () => query },
        '../database/schema': { deviceTrafficHourly: {}, devices: {} },
        './metrics': mskDay,
      }, { process },
    )
    expect(await activityDays(NOW)).toEqual({
      today: mskDay.mskDay(NOW),
      days: [
        { day: 20740, online: 1, bytes: 6 * MIN - 1 },
        { day: 20741, online: 1, bytes: MIN + 3 },
      ],
    })
  })
})

test.describe('traffic-collector', () => {
  for (const proto of ['wg', 'ovpn']) {
    test(`сборщик ${proto}: ошибка записи не теряет трафик, повтор не удваивает его`, async () => {
      const client = createClient({ url: ':memory:' })
      const db = drizzle(client, { schema })
      try {
        await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
        const [owner] = await db.insert(schema.clients).values({ name: 'collector-test', password: 'unused-test-password' }).returning()
        const [device] = await db.insert(schema.devices).values({ clientId: owner!.id, name: 'test', wgPublicKey: 'test-key' }).returning()
        let counter = 100
        const collector = serverModule<typeof import('../server/utils/traffic')>(
          new URL('../server/utils/traffic.ts', import.meta.url), {
            '../database/client': { useDb: () => db },
            '../database/schema': schema,
            './api': { coalesceRuns },
            './metrics': mskDay,
            './control': { control: async (_method: string, params: { protocol: string }) => {
              if (params.protocol === 'ikev2') throw new Error('no IKEv2 sample')
              return { samples: params.protocol === (proto === 'ovpn' ? 'openvpn' : proto)
                ? [{ key: 'test-key', device_id: device!.id, rx: counter * 2, tx: counter }] : [] }
            } },
            'node:child_process': {
              execFile: (...args: unknown[]) => {
                const callback = args.at(-1) as (err: Error | null, output: { stdout: string }) => void
                callback(null, { stdout: proto === 'wg' ? `test-key\t${counter}\t${counter * 2}\n` : '' })
              },
            },
            'node:fs/promises': {
              readFile: async (path: string) => {
                if (path.includes('status-server')) return proto === 'ovpn' ? `CLIENT_LIST,dev_${device!.id},addr,ip,,${counter},${counter * 2}\n` : ''
                throw new Error('no IKEv2 status file')
              },
            },
          }, { useLogger: () => ({ info: () => {} }), process },
        )
        await collector.collectTraffic()
        counter = 200
        await client.execute("CREATE TRIGGER full_disk BEFORE INSERT ON device_traffic_hourly BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
        await expect(Promise.resolve(collector.collectTraffic())).rejects.toThrow()
        expect((await db.select().from(schema.devices))[0]!.rxTotal).toBe(0)
        await client.execute('DROP TRIGGER full_disk')
        counter = 300
        await Promise.all([collector.collectTraffic(), collector.collectTraffic(), collector.collectTraffic()])
        const [saved] = await db.select().from(schema.devices)
        expect({ rx: saved!.rxTotal, tx: saved!.txTotal }).toEqual({ rx: 400, tx: 200 })
        const [hourly] = await db.select().from(schema.deviceTrafficHourly)
        expect({ rx: hourly!.rx, tx: hourly!.tx }).toEqual({ rx: 400, tx: 200 })
        expect(collector.recentBytesByDevice().get(device!.id)).toBe(600)
      }
      finally {
        client.close()
      }
    })
  }

  test('история трафика: год хранится, сутки старше двух недель свёрнуты, суммы по суткам и периодам прежние', async () => {
    const client = createClient({ url: ':memory:' })
    const db = drizzle(client, { schema })
    try {
      await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
      const ids: number[] = []
      for (const name of ['history-a', 'history-b']) {
        const [owner] = await db.insert(schema.clients).values({ name, password: 'unused-test-password' }).returning()
        for (const dev of ['phone', 'laptop']) {
          const [d] = await db.insert(schema.devices).values({ clientId: owner!.id, name: dev }).returning()
          ids.push(d!.id)
        }
      }
      const nowSec = Math.floor(Date.now() / 1000)
      const hourNow = Math.floor(nowSec / 3600) * 3600
      // 360 суток каждый час (до 3 МБ за час - часть суток выше порога «активен»)
      // и двое суток за пределами года, которые срок хранения должен удалить.
      const rows: Array<typeof schema.deviceTrafficHourly.$inferInsert> = []
      for (const [i, deviceId] of ids.entries()) {
        for (let h = 0; h < 360 * 24; h++) {
          const v = ((h * 7919 + i * 104729) % 1000) * 3000
          rows.push({ deviceId, hour: hourNow - h * 3600, rx: v, tx: v >> 3 })
        }
        for (const days of [370, 371]) rows.push({ deviceId, hour: hourNow - days * 86400, rx: 5, tx: 5 })
      }
      for (let i = 0; i < rows.length; i += 2000) await db.insert(schema.deviceTrafficHourly).values(rows.slice(i, i + 2000))

      // Каждая загрузка - как свежий запуск панели: первый прогон сборщика чистит историю.
      const loadCollector = () => serverModule<typeof import('../server/utils/traffic')>(
        new URL('../server/utils/traffic.ts', import.meta.url), {
          '../database/client': { useDb: () => db },
          '../database/schema': schema,
          './api': { coalesceRuns },
          './metrics': mskDay,
          './control': { control: async () => { throw new Error('no traffic sample') } },
          'node:child_process': { execFile: (...args: unknown[]) => (args.at(-1) as (e: Error | null, o: { stdout: string }) => void)(null, { stdout: '' }) },
          'node:fs/promises': { readFile: async () => { throw new Error('no status file') } },
        }, { useLogger: () => ({ info: () => {} }), process },
      )
      const collector = loadCollector()
      const activity = collector
      const { activityDays } = collector
      const consumers = async () => ({
        days: (await activityDays()).days,
        day: [...(await activity.periodTotalsByClient(activity.periodStartSec('day'))).entries()],
        week: [...(await activity.periodTotalsByClient(activity.periodStartSec('week'))).entries()],
      })
      const table = () => db.select().from(schema.deviceTrafficHourly).orderBy(schema.deviceTrafficHourly.deviceId, schema.deviceTrafficHourly.hour)
      const cutoff = mskDay.mskDayStartSec(nowSec * 1000, 14)
      const before = await consumers()
      const recentBefore = (await table()).filter(r => r.hour >= cutoff)

      await collector.collectTraffic()

      const after = await consumers()
      const yearAgo = mskDay.mskDay((nowSec - 365 * 86400) * 1000)
      expect(after.days).toEqual(before.days.filter(d => d.day > yearAgo))
      expect(after.days.length).toBeGreaterThan(355)
      expect(after.day).toEqual(before.day)
      expect(after.week).toEqual(before.week)
      const compacted = await table()
      // Последние две недели - по часам как были, старше - запись на сутки в 00:00 МСК.
      expect(compacted.filter(r => r.hour >= cutoff)).toEqual(recentBefore)
      const old = compacted.filter(r => r.hour < cutoff)
      expect(old.every(r => r.hour === mskDay.mskDayStartSec(r.hour * 1000))).toBe(true)
      expect(old.length).toBe(new Set(old.map(r => `${r.deviceId}:${r.hour}`)).size)
      expect(compacted.length).toBeLessThan(rows.length / 10)

      // Повтор после перезапуска на уже свёрнутой базе ничего не меняет.
      await loadCollector().collectTraffic()
      expect(await table()).toEqual(compacted)
    }
    finally {
      client.close()
    }
  })
})

test.describe('activity-scale', () => {
  // Ступени сетки «Активность» на панелях разного размера: 3, 18 и 300 клиентов,
  // 120 показанных дней, первые 10 — до установки (нули). Данные детерминированы.
  const { activityScale } = serverModule<typeof import('../app/utils/activity-scale')>('app/utils/activity-scale.ts', {})

  function rng(seed: number) {
    return () => {
      seed = (seed + 0x6D2B79F5) | 0
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    }
  }

  function panel(clients: number) {
    const r = rng(clients)
    const online: number[] = [], bytes: number[] = []
    for (let day = 0; day < 120; day++) {
      if (day < 10) { online.push(0); bytes.push(0); continue }
      const p = day % 7 >= 5 ? 0.8 : 0.6
      let n = 0, b = 0
      for (let c = 0; c < clients; c++) {
        if (r() >= p) continue
        n++
        b += 1.5e9 * Math.exp(r() * 2 - 1)
      }
      if (day === 100) b *= 3 // один день с большим скачиванием
      online.push(n); bytes.push(Math.round(b))
    }
    return { online, bytes }
  }

  for (const clients of [3, 18, 300]) {
    const data = panel(clients)
    for (const [mode, values] of [['Онлайн', data.online], ['Трафик', data.bytes]] as const) {
      test(`${clients} клиентов, ${mode}: дни расходятся по всем ступеням`, () => {
        const step = activityScale(values)
        const count = [0, 0, 0, 0, 0]
        for (const v of values) count[step(v)]!++
        const nonzero = values.filter(v => v > 0)
        console.log(`${clients} ${mode}: ${count.join(' / ')}`)

        expect(values.filter(v => v === 0).every(v => step(v) === 0)).toBe(true)
        expect(nonzero.every(v => step(v) > 0)).toBe(true)
        expect(step(Math.max(...values))).toBe(4)
        const sorted = [...values].sort((a, b) => a - b)
        for (let i = 1; i < sorted.length; i++) expect(step(sorted[i]!)).toBeGreaterThanOrEqual(step(sorted[i - 1]!))

        // ступеней не больше, чем различимых значений; иначе задействованы все
        const distinct = new Set(nonzero).size
        const used = count.slice(1).filter(n => n > 0).length
        expect(used).toBe(Math.min(4, distinct))
        // ни одна ступень не забирает больше половины ненулевых дней
        expect(Math.max(...count.slice(1))).toBeLessThanOrEqual(nonzero.length / 2)
      })
    }
  }
})
