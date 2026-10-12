import { test, expect } from '@playwright/test'
import * as mskDay from '../server/utils/msk-day'
import { serverModule } from './support/server-module'

const instance = '192.0.2.1:9100'
const midnight = Date.parse('2026-10-12T21:00:00Z')

function dailyMetrics(start: number) {
  let now = start
  let missing = false
  const queries: Array<{ query: string, time?: number }> = []
  const metrics = serverModule<typeof import('../server/utils/vm-client')>(
    new URL('../server/utils/vm-client.ts', import.meta.url), {
      './logger': { useLogger: () => ({ warn: () => {} }) },
      './msk-day': mskDay,
    }, {
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
