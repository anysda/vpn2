import { MSK_OFFSET_SEC } from '../../shared/msk'
import { parseOutbound } from '../../shared/outbounds'

/** Сутки панели с 00:00 МСК: суточный трафик нод и клиентов обнуляется в один момент. */
export const mskDay = (ms: number) => Math.floor((ms / 1000 + MSK_OFFSET_SEC) / 86400)

export const mskDayStartSec = (ms: number, daysBack = 0) =>
  (mskDay(ms) - daysBack) * 86400 - MSK_OFFSET_SEC

export const PROMQL = {
  cpu: '100 * sum by(instance) (rate(node_cpu_seconds_total{mode=~"user|system|softirq|irq|nice"}[30s])) / count by(instance) (node_cpu_seconds_total{mode="idle"})',
  iowait: '100 * avg by(instance) (rate(node_cpu_seconds_total{mode="iowait"}[30s]))',
  steal: '100 * avg by(instance) (rate(node_cpu_seconds_total{mode="steal"}[30s]))',
  ram: '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)',
  rx: 'sum by(instance) (irate(node_network_receive_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  tx: 'sum by(instance) (irate(node_network_transmit_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  uptime: 'node_time_seconds - node_boot_time_seconds',
  diskFree: 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
  diskSize: 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
  cores: 'count by(instance)(node_cpu_seconds_total{mode="idle"})',
  memTotal: 'node_memory_MemTotal_bytes',
  stale: 'time() - timestamp(node_time_seconds)',
}

/** Последние удачные значения: instance → метрика → число. */
const holdover = new Map<string, Map<string, number>>()

function held(current: Map<string, number>, instance: string, key: string, allowHoldover = true): number | null {
  const value = current.get(instance)
  const memory = holdover.get(instance) ?? new Map<string, number>()
  holdover.set(instance, memory)
  if (value !== undefined) memory.set(key, value)
  return value ?? (allowHoldover ? memory.get(key) ?? null : null)
}

async function vmResult<V>(path: string, query: Record<string, unknown>, timeout: number, strict: boolean) {
  const data = await $fetch<{ status: string, data: { result: Array<{ metric: Record<string, string> } & V> } }>(
    `${useRuntimeConfig().vmUrl}/api/v1/${path}`, { query, timeout })
  if (data.status === 'success') return data.data.result.filter(row => row.metric.instance)
  if (strict) throw new Error(`VM query status ${data.status}`)
  return []
}

/** Значение по instance; недоступная VM - пустая карта, при strict - исключение. */
export async function instantQuery(promql: string, time?: number, strict = false): Promise<Map<string, number>> {
  const result = new Map<string, number>()
  try {
    const rows = await vmResult<{ value: [number, string] }>('query', { query: promql, ...(time === undefined ? {} : { time }) }, 3000, strict)
    for (const row of rows) {
      const value = Number(row.value[1])
      if (Number.isFinite(value)) result.set(row.metric.instance!, value)
    }
  }
  catch (err) {
    if (strict) throw err
    useLogger().warn({ err, promql }, 'VM query failed')
  }
  return result
}

/** Ряды по instance на сетке start..end с шагом step, пропуск - null. */
export async function rangeQuery(promql: string, start: number, end: number, step: number): Promise<Map<string, Array<number | null>>> {
  const n = Math.floor((end - start) / step) + 1
  const out = new Map<string, Array<number | null>>()
  try {
    for (const row of await vmResult<{ values: Array<[number, string]> }>('query_range', { query: promql, start, end, step: `${step}s` }, 5000, false)) {
      const series: Array<number | null> = Array.from({ length: n }, () => null)
      for (const [t, v] of row.values) {
        const i = Math.round((Number(t) - start) / step)
        if (i >= 0 && i < n && Number.isFinite(Number(v))) series[i] = Number(v)
      }
      out.set(row.metric.instance!, series)
    }
  }
  catch (err) {
    useLogger().warn({ err, promql }, 'VM range query failed')
  }
  return out
}

/** Без holdover: для журнала событий. Недоступная VM - исключение, а не «все ноды оффлайн». */
async function fetchNodeHealth(instances: string[]) {
  const [stale, iowait, uptime] = await Promise.all([PROMQL.stale, PROMQL.iowait, PROMQL.uptime].map(q => instantQuery(q, undefined, true)))
  return instances.map(instance => ({
    instance,
    staleSec: stale!.get(instance) ?? null,
    iowaitPct: iowait!.get(instance) ?? null,
    uptimeSec: uptime!.get(instance) ?? null,
  }))
}

const NODE_FIELDS = {
  cpuPct: 'cpu', iowaitPct: 'iowait', stealPct: 'steal', ramPct: 'ram', rxBps: 'rx', txBps: 'tx', uptimeSec: 'uptime',
  diskFreeBytes: 'diskFree', diskSizeBytes: 'diskSize', cores: 'cores', memTotalBytes: 'memTotal',
} as const
type NodeField = keyof typeof NODE_FIELDS

/** Holdover нужен интерфейсу, но не тревогам бота: staleSec всегда настоящий. */
export async function fetchNodeMetrics(instances: string[], allowHoldover = true) {
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

// Суточный трафик - только физические интерфейсы: на entry трафик клиента виден ещё на wg0/tun0/xfrm0.
export const DAILY_DEVICE_FILTER = 'device!~"lo|wg.*|tun.*|xfrm.*|docker.*|veth.*|br-.*"'
const DAILY_CACHE_MS = 30_000

let dailyCache: { at: number, day: number, rx: Map<string, number>, tx: Map<string, number> } | null = null

export async function fetchDailyTraffic(instances: string[]): Promise<Map<string, { rxBytes: number | null, txBytes: number | null }>> {
  const now = Date.now()
  const day = mskDay(now)
  if (dailyCache && dailyCache.day !== day) {
    dailyCache = null
    for (const m of holdover.values()) { m.delete('rxDay'); m.delete('txDay') }
  }
  if (!dailyCache || now - dailyCache.at > DAILY_CACHE_MS) {
    const seconds = (Math.floor(now / 1000) + MSK_OFFSET_SEC) % 86400
    if (seconds === 0) return new Map(instances.map(inst => [inst, { rxBytes: 0, txBytes: 0 }]))
    const range = `${seconds}s`
    // increase_prometheus не берёт сэмпл из вчерашнего дня; время фиксируем вместе с окном.
    const time = Math.floor(now / 1000)
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

/** Ноды из NUXT_EXIT_TAGS и их mgmt-адреса из NUXT_MGMT_IPS ("tag:ip,tag:ip"). */
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

const RX = `node_network_receive_bytes_total{${DAILY_DEVICE_FILTER}}`
const increase = (sec: number) => `sum by(instance) (increase_prometheus(${RX}[${sec}s]))`

const round = (v: number | null | undefined) => v == null ? null : Math.round(v)

const SHAPE = { day: { step: 3600, n: 24 }, week: { step: 86400, n: 7 } } as const

/**
 * Трафик экзитов за сутки (24 часа) или неделю (7 суток МСК). Полные вёдра - query_range, текущее -
 * мгновенный запрос от его начала; prev - то же окно периодом раньше.
 */
export async function exitTraffic(period: 'day' | 'week', nowMs = Date.now()) {
  const { step, n } = SHAPE[period]
  const now = Math.floor(nowMs / 1000)
  const cur = period === 'day' ? Math.floor(now / 3600) * 3600 : mskDay(nowMs) * 86400 - MSK_OFFSET_SEC
  const first = cur - (n - 1) * step
  const elapsed = now - cur
  const window = now - first
  const exits = nodeInstances().filter(x => x.tag !== 'ru')

  const [full, partial, before] = await Promise.all([
    rangeQuery(increase(step), first + step, cur, step),
    elapsed > 0 ? instantQuery(increase(elapsed), now) : Promise.resolve(new Map<string, number>()),
    instantQuery(increase(window), now - n * step),
  ])

  const rows = exits.map(({ tag, instance }) => {
    const done = full.get(instance) ?? []
    const bytes = Array.from({ length: n - 1 }, (_, i) => round(done[i]))
    bytes.push(elapsed > 0 ? round(partial.get(instance)) : 0)
    return { tag, bytes }
  })
  const total = Array.from({ length: n }, (_, i) => rows.reduce((s, r) => s + (r.bytes[i] ?? 0), 0))
  const prevs = exits.map(x => round(before.get(x.instance))).filter((v): v is number => v !== null)

  return {
    period,
    step,
    starts: Array.from({ length: n }, (_, i) => first + i * step),
    exits: rows,
    total,
    sum: total.reduce((a, b) => a + b, 0),
    prev: prevs.length ? prevs.reduce((a, b) => a + b, 0) : null,
  }
}

export interface ClashProxy { name: string, type: string, now?: string, delay?: number, history?: Array<{ time: string, delay: number }> }

/** clash API sing-box отдаёт JSON с `Content-Type: text/plain`: разбор явный. */
function clash<T>(path: string): Promise<T> {
  const secret = useRuntimeConfig().clashSecret as string
  return $fetch<T>(`${useRuntimeConfig().clashApiUrl}/${path}`, {
    parseResponse: JSON.parse, timeout: 3000, headers: secret ? { Authorization: `Bearer ${secret}` } : {},
  }) as Promise<T>
}

export async function fetchProxies(): Promise<ClashProxy[]> {
  try {
    return Object.values((await clash<{ proxies?: Record<string, ClashProxy> }>('proxies')).proxies ?? {})
  }
  catch (err) {
    useLogger().warn({ err }, 'clash /proxies failed')
    return []
  }
}

export async function fetchConnections() {
  try {
    return await clash<{ connections: Array<{ chains: string[], metadata?: { sourceIP?: string } }> }>('connections')
  }
  catch (err) {
    useLogger().warn({ err }, 'clash /connections failed')
    return null
  }
}

/** Выходы для панели, бота и маршрутов: direct-ru, затем direct и warp экзитов; служебные не показываются. */
export function userVisibleOutbounds(proxies: ClashProxy[]): ClashProxy[] {
  return proxies
    .filter(p => parseOutbound(p.name) !== null)
    .sort((a, b) => {
      if (a.name === 'direct-ru') return -1
      if (b.name === 'direct-ru') return 1
      const aWarp = a.name.endsWith('-warp')
      const bWarp = b.name.endsWith('-warp')
      if (aWarp !== bWarp) return aWarp ? 1 : -1
      return a.name.localeCompare(b.name)
    })
}

export async function readDisabledExits(): Promise<string[]> {
  return (await control<{ config: { disabled: string[] } }>('settings.read', { resource: 'disabled-exits' })).config.disabled
}

const exitQueue = serialQueue()

/**
 * Чтение-изменение-запись по очереди. false - не записано: выключался бы последний включённый
 * экзит. `written` идёт в той же очереди с прежним состоянием: журнал в порядке переключений.
 */
export function toggleExit(tag: string, disabled: boolean, exits: string[], written?: (wasDisabled: boolean) => Promise<unknown>): Promise<boolean> {
  return exitQueue(async () => {
    const current = new Set(await readDisabledExits())
    const wasDisabled = current.has(tag)
    if (disabled) current.add(tag)
    else current.delete(tag)
    if (exits.every(t => current.has(t))) return false
    const kept = [...new Set([...current].filter(t => exits.includes(t)))].sort()
    await controlApply('settings.apply', { resource: 'disabled-exits', config: { disabled: kept } })
    await written?.(wasDisabled)
    return true
  })
}

export function exitNodeOf(outbound: string | undefined | null): string | null {
  if (!outbound || outbound === 'direct-ru') return null
  return parseOutbound(outbound)?.tag ?? null
}

// Сторож нод для ленты: пишет только переходы состояния.
export const NODE_CHECK_MS = 30_000
const LAG_SEC = 60
const OFFLINE_SEC = 180
const IOWAIT_PCT = 30
// Пропавшая серия (рестарт VM, сбой скрейпа) - оффлайн только со второй проверки подряд.
const MISSING_HITS = 2
const NODE_TYPES = ['node_offline', 'node_lag', 'node_online']
const LAG_TO_OFFLINE_MS = OFFLINE_SEC * 1000
const lagDetail = (staleSec: number) => `нет данных ${Math.round(staleSec)} с · ${CHECK_NOTE}`
const LAG_STALE_RE = /^нет данных (\d+) с/

type NodeState = 'online' | 'lagging' | 'offline'

interface Track {
  state: NodeState
  // Начало простоя - момент последней полученной метрики, а не проверки, заметившей потерю.
  since: number
  lastSeen: number | null
  missing: number
  ioHits: number
  ioAlerted: boolean
}

const tracks = new Map<string, Track>()
let activeExit: string | null = null

const nodeObject = (tag: string) => `node:${tag}`
const nodeName = (tag: string) => tag.toUpperCase()
const CHECK_NOTE = 'проверка каждые 30 с'

async function trackOf(tag: string): Promise<Track> {
  const known = tracks.get(tag)
  if (known) return known
  // После рестарта панели состояние и начало простоя - из журнала; «нет данных N с» в записи «отстают».
  const last = await lastEventOf(nodeObject(tag), NODE_TYPES)
  const state: NodeState = last?.type === 'node_offline' ? 'offline' : last?.type === 'node_lag' ? 'lagging' : 'online'
  let since = last?.at ?? 0
  if (last && state === 'lagging') since = lagStart(last)
  if (last && state === 'offline') {
    const lag = await lastEventOf(nodeObject(tag), ['node_lag'])
    if (lag && lag.at <= last.at && last.at - lag.at <= LAG_TO_OFFLINE_MS) since = lagStart(lag)
  }
  const track = { state, since, lastSeen: null, missing: 0, ioHits: 0, ioAlerted: false }
  tracks.set(tag, track)
  return track
}

function lagStart(lag: { at: number, detail?: string }): number {
  const stale = LAG_STALE_RE.exec(lag.detail ?? '')
  return stale ? lag.at - Number(stale[1]) * 1000 : lag.at
}

function downtimeText(ms: number): string {
  return `простой ${Math.max(1, Math.floor(ms / 60_000))} мин`
}

export async function checkNodes(now = Date.now()): Promise<void> {
  const nodes = nodeInstances()
  const health = await fetchNodeHealth(nodes.map(n => n.instance))
  for (const node of nodes) {
    const h = health.find(x => x.instance === node.instance)
    if (!h) continue
    const track = await trackOf(node.tag)
    const object = nodeObject(node.tag)
    const name = nodeName(node.tag)

    let observed: NodeState | null
    if (h.staleSec === null) {
      track.missing++
      observed = track.missing >= MISSING_HITS ? 'offline' : null
    }
    else {
      track.missing = 0
      track.lastSeen = now - h.staleSec * 1000
      observed = h.staleSec >= OFFLINE_SEC ? 'offline' : h.staleSec >= LAG_SEC ? 'lagging' : 'online'
    }

    if (observed === 'offline' && track.state !== 'offline') {
      if (track.state === 'online') track.since = track.lastSeen ?? now
      track.state = 'offline'
      await recordEvent({ type: 'node_offline', level: 'alarm', object, at: now, title: `${name} оффлайн`, detail: `нет метрик · ${CHECK_NOTE}` })
    }
    else if (observed === 'lagging' && track.state === 'online') {
      track.state = 'lagging'
      track.since = track.lastSeen ?? now
      await recordEvent({ type: 'node_lag', level: 'alarm', object, at: now, title: `${name}: метрики отстают`, detail: lagDetail(h.staleSec!) })
    }
    else if (observed === 'online' && track.state !== 'online') {
      const wasOffline = track.state === 'offline'
      const downMs = now - track.since
      track.state = 'online'
      track.since = now
      if (wasOffline) {
        const rebooted = h.uptimeSec !== null && h.uptimeSec < downMs / 1000 + 60
        await recordEvent({
          type: 'node_online', level: 'ok', object, at: now, title: `${name} снова в сети`,
          detail: [rebooted && 'после перезагрузки', downtimeText(downMs)].filter(Boolean).join(' · '),
        })
      }
    }

    if (h.iowaitPct !== null && h.iowaitPct > IOWAIT_PCT) {
      track.ioHits++
      if (track.ioHits >= 2 && !track.ioAlerted) {
        track.ioAlerted = true
        await recordEvent({ type: 'node_iowait', level: 'alarm', object, at: now, title: `${name}: метрики отстают`, detail: `iowait ${Math.round(h.iowaitPct)}% · ${CHECK_NOTE}` })
      }
    }
    else {
      track.ioHits = 0
      if (h.iowaitPct !== null) track.ioAlerted = false
    }
  }
}

export async function checkFailover(now = Date.now()): Promise<void> {
  const proxies = await fetchProxies()
  const current = exitNodeOf(proxies.find(p => p.name === 'foreign-best')?.now)
  if (!current) return
  if (activeExit && activeExit !== current) {
    await recordEvent({
      type: 'failover', level: 'alarm', object: 'foreign-best', at: now,
      title: 'Сторож переключил экзит', detail: `${nodeName(activeExit)} → ${nodeName(current)}`,
    })
  }
  activeExit = current
}
