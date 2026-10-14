import { test, expect } from '@playwright/test'
import * as mskDay from '../server/utils/msk-day'
import { serverModule } from './support/server-module'

const NL = '192.0.2.1:9100', FI = '192.0.2.2:9100', RU = '192.0.2.9:9100'

interface Call { promql: string, start?: number, end?: number, step?: number, time?: number }
type Rows = Map<string, Array<number | null>>

// Настоящий exit-traffic.ts с подменой VM: range — ответ query_range,
// instant — ответы мгновенных запросов по порядку (текущее ведро, затем прошлое окно).
function load(range: Rows, instant: Array<Map<string, number>>, exits = [{ tag: 'ru', instance: RU }, { tag: 'nl', instance: NL }, { tag: 'fi', instance: FI }]) {
  const calls: { range: Call[], instant: Call[] } = { range: [], instant: [] }
  const mod = serverModule<typeof import('../server/utils/exit-traffic')>(
    new URL('../server/utils/exit-traffic.ts', import.meta.url), {
      './msk-day': mskDay,
      './vm-range': {
        rangeQuery: async (promql: string, start: number, end: number, step: number) => {
          calls.range.push({ promql, start, end, step })
          return range
        },
      },
      './vm-client': {
        DAILY_DEVICE_FILTER: 'device="eth0"',
        nodeInstances: () => exits,
        instantQuery: async (promql: string, time?: number) => {
          calls.instant.push({ promql, time })
          return instant[calls.instant.length - 1] ?? new Map()
        },
      },
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
  const { activityDays } = serverModule<typeof import('../server/utils/activity-days')>(
    new URL('../server/utils/activity-days.ts', import.meta.url), {
      'drizzle-orm': { eq: () => null, sql: () => null },
      '../database/client': { useDb: () => query },
      '../database/schema': { deviceTrafficHourly: {}, devices: {} },
      './client-activity': { PERIOD_ACTIVE_MIN_BYTES: MIN },
      './msk-day': mskDay,
    },
  )
  expect(await activityDays(NOW)).toEqual({
    today: mskDay.mskDay(NOW),
    days: [
      { day: 20740, online: 1, bytes: 6 * MIN - 1 },
      { day: 20741, online: 1, bytes: MIN + 3 },
    ],
  })
})
