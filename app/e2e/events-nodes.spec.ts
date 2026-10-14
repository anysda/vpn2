import { test, expect } from '@playwright/test'
import * as mskDay from '../server/utils/msk-day'
import * as outbounds from '../shared/outbounds'
import { serverModule } from './support/server-module'

type Row = { type: string, level: string, object?: string | null, title: string, detail?: string, at?: number }
type Watch = typeof import('../server/utils/node-watch')

const INSTANCE = '192.0.2.2:9100'

/** Фейковая VictoriaMetrics: значения на ноду задаёт тест, down — VM недоступна. */
class FakeVm {
  down = false
  stale: number | null = 5
  iowait: number | null = 1
  uptime: number | null = 100_000

  fetch = async (_url: string, opts: { query: { query: string } }) => {
    if (this.down) throw new Error('connect ECONNREFUSED')
    const q = opts.query.query
    const value = q.includes('timestamp(node_time_seconds)') ? this.stale
      : q.includes('iowait') ? this.iowait
        : q.includes('node_boot_time_seconds') ? this.uptime
          : null
    return {
      status: 'success',
      data: { resultType: 'vector', result: value === null ? [] : [{ metric: { instance: INSTANCE }, value: [0, String(value)] }] },
    }
  }
}

function setup(seed: Row[] = [], foreignBest: { now: string | undefined } = { now: undefined }) {
  const vm = new FakeVm()
  const journal: Row[] = [...seed]
  const metrics = serverModule<typeof import('../server/utils/vm-client')>(
    new URL('../server/utils/vm-client.ts', import.meta.url), {
      './logger': { useLogger: () => ({ warn: () => {} }) },
      './msk-day': mskDay,
    }, {
      useRuntimeConfig: () => ({ vmUrl: 'http://metrics.invalid', mgmtIps: 'de:192.0.2.2', exitTags: 'de' }),
      $fetch: (url: string, opts: { query: { query: string } }) => vm.fetch(url, opts),
    },
  )
  // Только экзит de: ru в mgmtIps не задан, его инстанс в VM пуст — фильтруем.
  const only = { ...metrics, nodeInstances: () => metrics.nodeInstances().filter(n => n.tag === 'de') }
  const watch = serverModule<Watch>(new URL('../server/utils/node-watch.ts', import.meta.url), {
    './vm-client': only,
    './events': {
      recordEvent: async (row: Row) => { journal.push(row); return true },
      lastEventOf: async (object: string, types: string[]) => {
        const hit = [...journal].reverse().find(r => r.object === object && types.includes(r.type))
        return hit ? { type: hit.type, at: hit.at ?? 0 } : null
      },
    },
    './clash-client': { fetchProxies: async () => [{ name: 'foreign-best', type: 'Selector', now: foreignBest.now }] },
    './exit-control': { exitNodeOf: (o: string | undefined) => (o ? outbounds.parseOutbound(o)?.tag ?? null : null) },
  })
  return { vm, journal, watch }
}

const MIN = 60_000

test('нода: оффлайн и возврат — по одной записи на переход, с простоем', async () => {
  const { vm, journal, watch } = setup()
  let t = 1_000_000_000_000
  await watch.checkNodes(t)
  expect(journal).toHaveLength(0)

  vm.stale = 400
  for (let i = 0; i < 4; i++) await watch.checkNodes(t += 30_000)
  expect(journal.map(r => r.type)).toEqual(['node_offline'])
  expect(journal[0]).toMatchObject({ level: 'alarm', object: 'node:de', title: 'DE оффлайн' })

  const downAt = journal[0]!.at!
  vm.stale = 2
  vm.uptime = 3 * 60
  t = downAt + 4 * MIN
  await watch.checkNodes(t)
  await watch.checkNodes(t + 30_000)
  expect(journal.map(r => r.type)).toEqual(['node_offline', 'node_online'])
  expect(journal[1]).toMatchObject({ level: 'ok', title: 'DE снова в сети', detail: 'после перезагрузки · простой 4 мин' })
})

test('нода: пропавшая серия — оффлайн только со второй проверки, одиночный провал не пишется', async () => {
  const { vm, journal, watch } = setup()
  let t = 2_000_000_000_000
  vm.stale = null
  await watch.checkNodes(t += 30_000)
  vm.stale = 3
  await watch.checkNodes(t += 30_000)
  expect(journal).toHaveLength(0)

  vm.stale = null
  await watch.checkNodes(t += 30_000)
  expect(journal).toHaveLength(0)
  await watch.checkNodes(t += 30_000)
  expect(journal.map(r => r.type)).toEqual(['node_offline'])

  // Возврат без перезагрузки: аптайм больше простоя.
  vm.stale = 1
  vm.uptime = 10 * 86400
  await watch.checkNodes(t + 2 * MIN)
  expect(journal[1]).toMatchObject({ type: 'node_online', detail: 'простой 2 мин' })
})

test('нода: отставание метрик больше 60 с — одна тревога, без повторов', async () => {
  const { vm, journal, watch } = setup()
  let t = 3_000_000_000_000
  vm.stale = 59
  await watch.checkNodes(t += 30_000)
  expect(journal).toHaveLength(0)
  vm.stale = 75
  await watch.checkNodes(t += 30_000)
  vm.stale = 105
  await watch.checkNodes(t + 30_000)
  expect(journal).toHaveLength(1)
  expect(journal[0]).toMatchObject({ type: 'node_lag', level: 'alarm', title: 'DE: метрики отстают', detail: 'нет данных 75 с · проверка каждые 30 с' })
})

test('нода: iowait выше 30% две проверки подряд — одна тревога на эпизод', async () => {
  const { vm, journal, watch } = setup()
  let t = 4_000_000_000_000
  vm.iowait = 34
  await watch.checkNodes(t += 30_000)
  vm.iowait = 5
  await watch.checkNodes(t += 30_000)
  vm.iowait = 34
  await watch.checkNodes(t += 30_000)
  expect(journal).toHaveLength(0)
  await watch.checkNodes(t += 30_000)
  await watch.checkNodes(t += 30_000)
  expect(journal).toEqual([expect.objectContaining({ type: 'node_iowait', detail: 'iowait 34% · проверка каждые 30 с' })])

  // Новый эпизод после спада — новая запись.
  vm.iowait = 10
  await watch.checkNodes(t += 30_000)
  vm.iowait = 40
  await watch.checkNodes(t += 30_000)
  await watch.checkNodes(t + 30_000)
  expect(journal.filter(r => r.type === 'node_iowait')).toHaveLength(2)
})

test('нода: недоступная VictoriaMetrics — не «все ноды оффлайн»', async () => {
  const { vm, journal, watch } = setup()
  vm.down = true
  for (let i = 0; i < 3; i++) {
    // Промис из чужого vm-контекста: матчер rejects его не узнаёт.
    const failed = await watch.checkNodes(5_000_000_000_000 + i * 30_000).then(() => false, () => true)
    expect(failed).toBe(true)
  }
  expect(journal).toHaveLength(0)
})

test('нода: рестарт панели не повторяет «оффлайн» и считает простой с начала', async () => {
  const downAt = 6_000_000_000_000
  const { vm, journal, watch } = setup([{ type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', at: downAt }])
  vm.stale = 500
  await watch.checkNodes(downAt + 10 * MIN)
  await watch.checkNodes(downAt + 11 * MIN)
  expect(journal).toHaveLength(1)
  vm.stale = 1
  await watch.checkNodes(downAt + 20 * MIN)
  expect(journal[1]).toMatchObject({ type: 'node_online', detail: 'простой 20 мин' })
})

test('нода: «отстают» → «оффлайн» → в сети — простой с первой потери метрик', async () => {
  const { vm, journal, watch } = setup()
  vm.uptime = 10 * 86400
  let t = 7_000_000_000_000
  await watch.checkNodes(t)
  vm.stale = 75
  await watch.checkNodes(t += 30_000)
  const lagAt = t
  vm.stale = 195
  await watch.checkNodes(t + 2 * MIN)
  expect(journal.map(r => r.type)).toEqual(['node_lag', 'node_offline'])
  vm.stale = 1
  await watch.checkNodes(lagAt + 4 * MIN + 10_000)
  expect(journal[2]).toMatchObject({ type: 'node_online', title: 'DE снова в сети', detail: 'простой 4 мин' })
})

test('нода: «отстают» → в сети, потом «оффлайн» напрямую — простой от «оффлайн»', async () => {
  const { vm, journal, watch } = setup()
  vm.uptime = 10 * 86400
  let t = 8_000_000_000_000
  vm.stale = 75
  await watch.checkNodes(t += 30_000)
  vm.stale = 2
  await watch.checkNodes(t += 30_000)
  // Отставание без «оффлайн» — возврат в ленту не пишется.
  expect(journal.map(r => r.type)).toEqual(['node_lag'])

  vm.stale = 400
  await watch.checkNodes(t += 30_000)
  const downAt = t
  vm.stale = 1
  await watch.checkNodes(downAt + 3 * MIN)
  expect(journal.map(r => r.type)).toEqual(['node_lag', 'node_offline', 'node_online'])
  expect(journal[2]).toMatchObject({ detail: 'простой 3 мин' })
})

test('нода: рестарт панели после «отстают» → «оффлайн» — простой с «отстают»', async () => {
  const lagAt = 9_000_000_000_000
  const { vm, journal, watch } = setup([
    { type: 'node_lag', level: 'alarm', object: 'node:de', title: 'DE: метрики отстают', at: lagAt },
    { type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', at: lagAt + 2 * MIN },
  ])
  vm.uptime = 10 * 86400
  vm.stale = 1
  await watch.checkNodes(lagAt + 6 * MIN)
  expect(journal[2]).toMatchObject({ type: 'node_online', detail: 'простой 6 мин' })
})

test('нода: рестарт панели — давнее «отстают» перед «оффлайн» к простою не относится', async () => {
  const lagAt = 9_500_000_000_000
  const downAt = lagAt + 60 * MIN
  const { vm, journal, watch } = setup([
    { type: 'node_lag', level: 'alarm', object: 'node:de', title: 'DE: метрики отстают', at: lagAt },
    { type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', at: downAt },
  ])
  vm.uptime = 10 * 86400
  vm.stale = 1
  await watch.checkNodes(downAt + 5 * MIN)
  expect(journal[2]).toMatchObject({ type: 'node_online', detail: 'простой 5 мин' })
})

test('failover: сторож увёл foreign-best на другой узел — тревога', async () => {
  const proxy = { now: 'hy2-de-direct' as string | undefined }
  const { journal, watch } = setup([], proxy)
  await watch.checkFailover(1)
  await watch.checkFailover(2)
  expect(journal).toHaveLength(0)
  proxy.now = 'hy2-nl-warp'
  await watch.checkFailover(3)
  // Clash недоступен — не переход.
  proxy.now = undefined
  await watch.checkFailover(4)
  proxy.now = 'hy2-nl-direct'
  await watch.checkFailover(5)
  expect(journal).toEqual([expect.objectContaining({ type: 'failover', level: 'alarm', title: 'Сторож переключил экзит', detail: 'DE → NL' })])
})
