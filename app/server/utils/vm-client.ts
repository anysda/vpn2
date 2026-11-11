import { useLogger } from './logger'
import { MSK_OFFSET_SEC, mskDay } from './msk-day'

interface VmResponse {
  status: 'success' | 'error'
  data: { result: Array<{ metric: Record<string, string>, value: [number, string] }> }
}

export const PROMQL = {
  // Занятость без iowait и steal; сумма по ядрам, делённая на их число: 100% = все ядра заняты.
  cpu: '100 * sum by(instance) (rate(node_cpu_seconds_total{mode=~"user|system|softirq|irq|nice"}[30s])) / count by(instance) (node_cpu_seconds_total{mode="idle"})',
  iowait: '100 * avg by(instance) (rate(node_cpu_seconds_total{mode="iowait"}[30s]))',
  steal: '100 * avg by(instance) (rate(node_cpu_seconds_total{mode="steal"}[30s]))',
  ram: '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)',
  rx: 'sum by(instance) (irate(node_network_receive_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  tx: 'sum by(instance) (irate(node_network_transmit_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  uptime: 'node_time_seconds - node_boot_time_seconds',
  // Корневая ФС: доступно (как df Avail) и всего.
  diskFree: 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
  diskSize: 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
  // Размер ноды: по ядрам и памяти бот выбирает пороги тревог.
  cores: 'count by(instance)(node_cpu_seconds_total{mode="idle"})',
  memTotal: 'node_memory_MemTotal_bytes',
  // Возраст последнего сэмпла, сек: по нему индикатор online/warning/offline.
  stale: 'time() - timestamp(node_time_seconds)',
}

/** Последние удачные значения: instance → метрика → число. */
const holdover = new Map<string, Map<string, number>>()

/** Текущее значение метрики; если его нет, то последнее удачное (когда holdover разрешён). */
function held(current: Map<string, number>, instance: string, key: string, allowHoldover = true): number | null {
  const value = current.get(instance)
  const memory = holdover.get(instance) ?? new Map<string, number>()
  holdover.set(instance, memory)
  if (value !== undefined) memory.set(key, value)
  return value ?? (allowHoldover ? memory.get(key) ?? null : null)
}

export async function instantQuery(promql: string, time?: number, strict = false): Promise<Map<string, number>> {
  const url = useRuntimeConfig().vmUrl as string
  const result = new Map<string, number>()
  try {
    const data = await $fetch<VmResponse>(`${url}/api/v1/query`, {
      query: { query: promql, ...(time === undefined ? {} : { time }) },
      timeout: 3000,
    })
    if (data.status !== 'success') {
      if (strict) throw new Error(`VM query status ${data.status}`)
      return result
    }
    for (const row of data.data.result) {
      const value = Number(row.value[1])
      if (row.metric.instance && Number.isFinite(value)) result.set(row.metric.instance, value)
    }
  }
  catch (err) {
    if (strict) throw err
    useLogger().warn({ err, promql }, 'VM query failed')
  }
  return result
}

export interface NodeHealth {
  instance: string
  staleSec: number | null
  iowaitPct: number | null
  uptimeSec: number | null
}

/** Состояние нод без holdover для журнала событий. Недоступная VM - исключение, а не «все ноды оффлайн». */
export async function fetchNodeHealth(instances: string[]): Promise<NodeHealth[]> {
  const [stale, iowait, uptime] = await Promise.all([PROMQL.stale, PROMQL.iowait, PROMQL.uptime].map(q => instantQuery(q, undefined, true)))
  return instances.map(instance => ({
    instance,
    staleSec: stale!.get(instance) ?? null,
    iowaitPct: iowait!.get(instance) ?? null,
    uptimeSec: uptime!.get(instance) ?? null,
  }))
}

/** Поле ответа → ключ PROMQL. staleSec отдельно: ему нужен настоящий возраст, без holdover. */
const NODE_FIELDS = {
  cpuPct: 'cpu', iowaitPct: 'iowait', stealPct: 'steal', ramPct: 'ram', rxBps: 'rx', txBps: 'tx', uptimeSec: 'uptime',
  diskFreeBytes: 'diskFree', diskSizeBytes: 'diskSize', cores: 'cores', memTotalBytes: 'memTotal',
} as const
type NodeField = keyof typeof NODE_FIELDS
export type NodeMetrics = { instance: string, staleSec: number | null } & Record<NodeField, number | null>

/** Holdover нужен интерфейсу, но не алертам: бот запрашивает только текущие значения. */
export async function fetchNodeMetrics(instances: string[], allowHoldover = true): Promise<NodeMetrics[]> {
  const fields = Object.keys(NODE_FIELDS) as NodeField[]
  const queries = [...fields.map(field => PROMQL[NODE_FIELDS[field]]), PROMQL.stale]
  const results = await Promise.all(queries.map(q => instantQuery(q)))
  return instances.map((instance) => {
    const values = fields.map((field, i) => [field, held(results[i]!, instance, NODE_FIELDS[field], allowHoldover)])
    return {
      instance,
      ...Object.fromEntries(values) as Record<NodeField, number | null>,
      staleSec: results[fields.length]!.get(instance) ?? null,
    }
  })
}

// Суточный трафик ноды считаем только по физическому интерфейсу: на entry
// трафик клиента виден ещё и на wg0/tun0/xfrm0, сумма удвоилась бы.
export const DAILY_DEVICE_FILTER = 'device!~"lo|wg.*|tun.*|xfrm.*|docker.*|veth.*|br-.*"'
const DAILY_CACHE_MS = 30_000

// increase() за сутки тяжёлый, а панель опрашивает ноды раз в 2 с: результат держим 30 с.
let dailyCache: { at: number, day: number, rx: Map<string, number>, tx: Map<string, number> } | null = null

export async function fetchDailyTraffic(instances: string[]): Promise<Map<string, { rxBytes: number | null, txBytes: number | null }>> {
  const now = Date.now()
  const day = mskDay(now)
  if (dailyCache && dailyCache.day !== day) {
    // Полночь МСК: ни кэш, ни holdover не должны показывать вчерашние сутки.
    dailyCache = null
    for (const m of holdover.values()) { m.delete('rxDay'); m.delete('txDay') }
  }
  if (!dailyCache || now - dailyCache.at > DAILY_CACHE_MS) {
    const seconds = (Math.floor(now / 1000) + MSK_OFFSET_SEC) % 86400
    // В первую секунду суток окно пустое: ноль без запроса и без кэша.
    if (seconds === 0) return new Map(instances.map(inst => [inst, { rxBytes: 0, txBytes: 0 }]))
    const range = `${seconds}s`
    const time = Math.floor(now / 1000)
    // increase_prometheus не берёт сэмпл из вчерашнего дня; время фиксируем вместе с окном.
    const [rx, tx] = await Promise.all([
      instantQuery(`sum by(instance) (increase_prometheus(node_network_receive_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]))`, time),
      instantQuery(`sum by(instance) (increase_prometheus(node_network_transmit_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]))`, time),
    ])
    dailyCache = { at: now, day, rx, tx }
  }
  return new Map(instances.map(inst => [inst, {
    rxBytes: held(dailyCache!.rx, inst, 'rxDay'),
    txBytes: held(dailyCache!.tx, inst, 'txDay'),
  }]))
}

/**
 * Ноды и их адреса в mgmt-сети из `NUXT_MGMT_IPS` ("tag:ip,tag:ip"). Индексы
 * адресов стабильны: исключённый экзит не сдвигает соседей.
 */
export function nodeInstances(): Array<{ tag: string, instance: string, label: string }> {
  const cfg = useRuntimeConfig()
  const tags = String(cfg.exitTags || '').trim().split(/\s+/).filter(Boolean)
  const ipByTag = new Map<string, string>()
  for (const pair of String(cfg.mgmtIps || '').split(',')) {
    const [tag, ip] = pair.split(':').map(s => s.trim())
    if (tag && ip) ipByTag.set(tag, ip)
  }
  return [
    { tag: 'ru', instance: `${ipByTag.get('ru') ?? '10.99.0.1'}:9100`, label: 'RU entry' },
    ...tags.filter(tag => ipByTag.has(tag)).map(tag => ({ tag, instance: `${ipByTag.get(tag)}:9100`, label: `${tag.toUpperCase()} exit` })),
  ]
}
