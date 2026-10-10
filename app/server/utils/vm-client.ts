import { useLogger } from './logger'
import { MSK_OFFSET_SEC, mskDay, mskDayStartSec } from './msk-day'

interface VmInstantResult {
  metric: Record<string, string>
  value: [number, string]
}

interface VmResponse {
  status: 'success' | 'error'
  data: {
    resultType: 'vector' | 'matrix' | 'scalar' | 'string'
    result: VmInstantResult[]
  }
}

export interface NodeMetrics {
  instance: string
  // Занятость ЦПУ (user+system+softirq+irq+nice), средняя по ядрам. Ожидание
  // диска и украденное гипервизором время сюда не входят — отдельные поля.
  cpuPct: number | null
  iowaitPct: number | null
  stealPct: number | null
  ramPct: number | null
  rxBps: number | null
  txBps: number | null
  uptimeSec: number | null
  // Корневая ФС ноды: доступно (как df Avail) и всего, байты.
  diskFreeBytes: number | null
  diskSizeBytes: number | null
  // Возраст последнего сэмпла метрик ноды, сек. null — данных нет вовсе.
  // Драйвит индикацию online/warning/offline (НЕ хранится в holdover —
  // нужен реальный возраст, иначе мёртвая нода вечно «свежая»).
  staleSec: number | null
}

const PROMQL = {
  // Сумма по ядрам, делённая на число ядер: 100% = все ядра заняты.
  cpu: '100 * sum by(instance) (rate(node_cpu_seconds_total{mode=~"user|system|softirq|irq|nice"}[30s])) / count by(instance) (node_cpu_seconds_total{mode="idle"})',
  iowait: '100 * avg by(instance) (rate(node_cpu_seconds_total{mode="iowait"}[30s]))',
  steal: '100 * avg by(instance) (rate(node_cpu_seconds_total{mode="steal"}[30s]))',
  ram: '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)',
  rx: 'sum by(instance) (irate(node_network_receive_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  tx: 'sum by(instance) (irate(node_network_transmit_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  uptime: 'node_time_seconds - node_boot_time_seconds',
  diskFree: 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
  diskSize: 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
  // Возраст последнего сэмпла: time() минус метка времени метрики.
  stale: 'time() - timestamp(node_time_seconds)',
}

// holdover[instance][metricKey] = last successful value
const holdover = new Map<string, Map<string, number>>()

function rememberValue(instance: string, metricKey: string, value: number) {
  let m = holdover.get(instance)
  if (!m) { m = new Map(); holdover.set(instance, m) }
  m.set(metricKey, value)
}

function recallValue(instance: string, metricKey: string): number | null {
  return holdover.get(instance)?.get(metricKey) ?? null
}

async function instantQuery(promql: string): Promise<Map<string, number>> {
  const url = useRuntimeConfig().vmUrl as string
  const result = new Map<string, number>()
  try {
    const data = await $fetch<VmResponse>(`${url}/api/v1/query`, {
      query: { query: promql },
      timeout: 3000,
    })
    if (data.status !== 'success') return result
    for (const row of data.data.result) {
      const instance = row.metric.instance
      if (!instance) continue
      const value = Number(row.value[1])
      if (!Number.isFinite(value)) continue
      result.set(instance, value)
    }
  }
  catch (err) {
    useLogger().warn({ err, promql }, 'VM query failed')
  }
  return result
}

function withHoldover(
  current: Map<string, number>,
  instances: string[],
  metricKey: string,
): Map<string, number | null> {
  const result = new Map<string, number | null>()
  for (const inst of instances) {
    const v = current.get(inst)
    if (v !== undefined) {
      rememberValue(inst, metricKey, v)
      result.set(inst, v)
    }
    else {
      result.set(inst, recallValue(inst, metricKey))
    }
  }
  return result
}

export async function fetchNodeMetrics(instances: string[]): Promise<NodeMetrics[]> {
  const [cpu, iowait, steal, ram, rx, tx, uptime, stale, diskFree, diskSize] = await Promise.all([
    instantQuery(PROMQL.cpu),
    instantQuery(PROMQL.iowait),
    instantQuery(PROMQL.steal),
    instantQuery(PROMQL.ram),
    instantQuery(PROMQL.rx),
    instantQuery(PROMQL.tx),
    instantQuery(PROMQL.uptime),
    instantQuery(PROMQL.stale),
    instantQuery(PROMQL.diskFree),
    instantQuery(PROMQL.diskSize),
  ])

  const cpuH = withHoldover(cpu, instances, 'cpu')
  const iowaitH = withHoldover(iowait, instances, 'iowait')
  const stealH = withHoldover(steal, instances, 'steal')
  const ramH = withHoldover(ram, instances, 'ram')
  const rxH = withHoldover(rx, instances, 'rx')
  const txH = withHoldover(tx, instances, 'tx')
  const upH = withHoldover(uptime, instances, 'uptime')
  const diskFreeH = withHoldover(diskFree, instances, 'diskFree')
  const diskSizeH = withHoldover(diskSize, instances, 'diskSize')

  return instances.map(inst => ({
    instance: inst,
    cpuPct: cpuH.get(inst) ?? null,
    iowaitPct: iowaitH.get(inst) ?? null,
    stealPct: stealH.get(inst) ?? null,
    ramPct: ramH.get(inst) ?? null,
    rxBps: rxH.get(inst) ?? null,
    txBps: txH.get(inst) ?? null,
    uptimeSec: upH.get(inst) ?? null,
    diskFreeBytes: diskFreeH.get(inst) ?? null,
    diskSizeBytes: diskSizeH.get(inst) ?? null,
    // staleSec — БЕЗ holdover: реальный возраст последнего сэмпла.
    staleSec: stale.get(inst) ?? null,
  }))
}

// Трафик ноды за период: increase() WAN-счётчиков. Фильтр строже, чем у
// rx/tx Mbps: на entry трафик клиента виден и на wg0/tun0/xfrm0, и на eth0 —
// считаем только физический интерфейс, иначе сумма удвоится.
const DAILY_DEVICE_FILTER = 'device!~"lo|wg.*|tun.*|xfrm.*|docker.*|veth.*|br-.*"'

export type TrafficPeriod = 'day' | 'yesterday' | 'week'

/** Секунд с 00:00 по Москве; не меньше 60, чтобы окно increase() не было пустым. */
function secondsSinceMskMidnight(nowMs = Date.now()): number {
  const sec = Math.floor(nowMs / 1000)
  return Math.max(60, (sec + MSK_OFFSET_SEC) % 86400)
}

/**
 * Окно и сдвиг increase() для периода (МСК): сутки — с полуночи; вчера —
 * 86400 с, сдвинутые на время с полуночи; неделя — с полуночи 6 суток назад.
 * VictoriaMetrics хранит 7 дней (-retentionPeriod=7d): неделя впритык.
 */
function periodWindow(period: TrafficPeriod, nowMs: number): { range: string, offset: string } {
  const sinceMidnight = secondsSinceMskMidnight(nowMs)
  if (period === 'yesterday') return { range: '86400s', offset: ` offset ${sinceMidnight}s` }
  if (period === 'week') return { range: `${sinceMidnight + 6 * 86400}s`, offset: '' }
  return { range: `${sinceMidnight}s`, offset: '' }
}

// increase() на 2с-скрейпе — тяжёлый запрос, а панель опрашивает /api/ops/nodes
// раз в 2с. Кэш по периоду: сутки — 30с, неделя — 5 мин, вчера не меняется до
// полуночи. На смене суток МСК кэш и holdover сбрасываются.
const PERIOD_CACHE_MS: Record<TrafficPeriod, number> = { day: 30_000, yesterday: 86_400_000, week: 300_000 }

export interface DailyTraffic {
  rxBytes: number | null
  txBytes: number | null
}

const periodCache = new Map<TrafficPeriod, { at: number, day: number, rx: Map<string, number>, tx: Map<string, number> }>()

export async function fetchPeriodTraffic(instances: string[], period: TrafficPeriod): Promise<Map<string, DailyTraffic>> {
  const now = Date.now()
  const day = mskDay(now)
  let cached = periodCache.get(period)
  if (cached && cached.day !== day) {
    // Полночь МСК: ни кэш, ни holdover не должны показывать прошлый период.
    periodCache.clear()
    cached = undefined
    for (const m of holdover.values()) {
      for (const k of [...m.keys()]) if (k.startsWith('rxP:') || k.startsWith('txP:')) m.delete(k)
    }
  }
  if (!cached || now - cached.at > PERIOD_CACHE_MS[period]) {
    const { range, offset } = periodWindow(period, now)
    const [rx, tx] = await Promise.all([
      instantQuery(`sum by(instance) (increase(node_network_receive_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]${offset}))`),
      instantQuery(`sum by(instance) (increase(node_network_transmit_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]${offset}))`),
    ])
    cached = { at: now, day, rx, tx }
    periodCache.set(period, cached)
  }
  const rxH = withHoldover(cached.rx, instances, `rxP:${period}`)
  const txH = withHoldover(cached.tx, instances, `txP:${period}`)
  return new Map(instances.map(inst => [inst, {
    rxBytes: rxH.get(inst) ?? null,
    txBytes: txH.get(inst) ?? null,
  }]))
}

interface VmRangeResponse {
  status: 'success' | 'error'
  data: { result: Array<{ metric: Record<string, string>, values: Array<[number, string]> }> }
}

/** query_range: instance → (unix-секунды точки → значение). Ошибка — пустая карта, как у instantQuery. */
async function rangeQuery(promql: string, start: number, end: number, step: number): Promise<Map<string, Map<number, number>>> {
  const url = useRuntimeConfig().vmUrl as string
  const result = new Map<string, Map<number, number>>()
  try {
    const data = await $fetch<VmRangeResponse>(`${url}/api/v1/query_range`, {
      query: { query: promql, start, end, step },
      timeout: 5000,
    })
    if (data.status !== 'success') return result
    for (const row of data.data.result) {
      const instance = row.metric.instance
      if (!instance) continue
      const points = new Map<number, number>()
      for (const [t, v] of row.values) {
        const value = Number(v)
        if (Number.isFinite(value)) points.set(Math.round(t), value)
      }
      result.set(instance, points)
    }
  }
  catch (err) {
    useLogger().warn({ err, promql }, 'VM range query failed')
  }
  return result
}

// Трафик ноды для диаграммы в карточке — по тому же периоду, что и итоги:
// сутки — 24 часа с 00:00 МСК (текущий час онлайн, будущие пустые), вчера —
// 24 часа вчерашних суток, неделя — 7 суток (сегодня онлайн). Основа — часы:
// объём — increase() за час, пик — максимальная средняя за минуту скорость
// внутри часа (подзапрос с шагом 30с; 2-секундный всплеск не рисует ложный
// пик). Сутки недели собираются из часов: объём — сумма, пик — максимум.
// Фильтр интерфейсов — как у трафика за период.
const HOUR = 3600
const DAY = 86400

type Dir = 'receive' | 'transmit'
const volumeExpr = (dir: Dir, range: string) =>
  `sum by(instance) (increase(node_network_${dir}_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]))`
const peakExpr = (dir: Dir, range: string) =>
  `max_over_time(sum by(instance) (rate(node_network_${dir}_bytes_total{${DAILY_DEVICE_FILTER}}[1m]))[${range}:30s])`

export interface TrafficBucket {
  // unix-секунды границ корзины [start, end) — час или сутки МСК
  start: number
  end: number
  // null — данных нет (нода лежала / VM не ответила / вне хранения VM), не ноль
  rxBytes: number | null
  txBytes: number | null
  rxPeakBps: number | null
  txPeakBps: number | null
  // текущая, ещё не закончившаяся корзина (обновляется онлайн)
  current: boolean
  // ещё не наступила (часы суток после текущего)
  future: boolean
}

// Полные часы периода не меняются до смены часа — запрос раз в час на период.
// Текущий час — кэш 5с (панель опрашивает раз в 5с, вкладок может быть несколько).
const fullHoursCache = new Map<TrafficPeriod, { hour: number, series: Map<string, Map<number, number>>[] }>()
let currentHourCache: { at: number, hour: number, series: Map<string, number>[] } | null = null
const CURRENT_CACHE_MS = 5_000

/** Часовая сетка периода: [from, slotsEnd), полные часы — до `to`, есть ли текущий час. */
function periodGrid(period: TrafficPeriod, nowSec: number) {
  const hour = Math.floor(nowSec / HOUR) * HOUR
  const today = mskDayStartSec(nowSec * 1000)
  if (period === 'yesterday') return { from: today - DAY, to: today, slotsEnd: today, hour, live: false }
  if (period === 'week') return { from: today - 6 * DAY, to: hour, slotsEnd: today + DAY, hour, live: true }
  return { from: today, to: hour, slotsEnd: today + DAY, hour, live: true }
}

async function fullHours(period: TrafficPeriod, from: number, to: number, hour: number) {
  const cached = fullHoursCache.get(period)
  if (cached?.hour === hour) return cached.series
  if (to <= from) return [] // сутки только начались — полных часов нет
  // Точка t в query_range — значение за (t−1ч, t]: точки from+1ч..to покрывают часы [from, to).
  const series = await Promise.all([
    rangeQuery(volumeExpr('receive', '1h'), from + HOUR, to, HOUR),
    rangeQuery(volumeExpr('transmit', '1h'), from + HOUR, to, HOUR),
    rangeQuery(peakExpr('receive', '1h'), from + HOUR, to, HOUR),
    rangeQuery(peakExpr('transmit', '1h'), from + HOUR, to, HOUR),
  ])
  // Пустой ответ (VM недоступна) не кэшируем — повторим на следующем опросе.
  if (series.some(m => m.size > 0)) fullHoursCache.set(period, { hour, series })
  return series
}

async function currentHour(hour: number, nowMs: number) {
  if (currentHourCache && currentHourCache.hour !== hour) {
    // Новый час: значения прошлого часа не должны всплыть через holdover.
    for (const m of holdover.values()) {
      for (const k of [...m.keys()]) if (k.startsWith('hourCur:')) m.delete(k)
    }
  }
  if (!currentHourCache || currentHourCache.hour !== hour || nowMs - currentHourCache.at > CURRENT_CACHE_MS) {
    const range = `${Math.max(60, Math.floor(nowMs / 1000) - hour)}s`
    const series = await Promise.all([
      instantQuery(volumeExpr('receive', range)),
      instantQuery(volumeExpr('transmit', range)),
      instantQuery(peakExpr('receive', range)),
      instantQuery(peakExpr('transmit', range)),
    ])
    currentHourCache = { at: nowMs, hour, series }
  }
  return currentHourCache.series
}

const sumOrNull = (vs: Array<number | null>) => (vs.every(v => v == null) ? null : vs.reduce<number>((a, v) => a + (v ?? 0), 0))
const maxOrNull = (vs: Array<number | null>) => (vs.every(v => v == null) ? null : Math.max(...vs.map(v => v ?? 0)))

/** Сутки МСК из часов: объём — сумма, пик — максимум; будущие часы не входят. */
function toDays(hours: TrafficBucket[], from: number): TrafficBucket[] {
  const days: TrafficBucket[] = []
  for (let start = from; start < from + 7 * DAY; start += DAY) {
    const hs = hours.filter(h => h.start >= start && h.start < start + DAY && !h.future)
    days.push({
      start,
      end: start + DAY,
      rxBytes: sumOrNull(hs.map(h => h.rxBytes)),
      txBytes: sumOrNull(hs.map(h => h.txBytes)),
      rxPeakBps: maxOrNull(hs.map(h => h.rxPeakBps)),
      txPeakBps: maxOrNull(hs.map(h => h.txPeakBps)),
      current: hs.some(h => h.current),
      future: false,
    })
  }
  return days
}

export async function fetchTrafficBuckets(instances: string[], period: TrafficPeriod): Promise<Map<string, TrafficBucket[]>> {
  const nowMs = Date.now()
  const { from, to, slotsEnd, hour, live } = periodGrid(period, Math.floor(nowMs / 1000))
  const [full, cur] = await Promise.all([
    fullHours(period, from, to, hour),
    live ? currentHour(hour, nowMs) : Promise.resolve(null),
  ])
  return new Map(instances.map((inst) => {
    // Текущий час — с holdover: короткий сбой VM не должен обнулять растущие столбцы.
    const curH = cur?.map((m, k) => withHoldover(m, [inst], `hourCur:${k}`).get(inst) ?? null)
    const hours: TrafficBucket[] = []
    for (let start = from; start < slotsEnd; start += HOUR) {
      const current = live && start === hour
      const future = live && start > hour
      const at = (k: number) => (current ? curH?.[k] ?? null : future ? null : full[k]?.get(inst)?.get(start + HOUR) ?? null)
      hours.push({ start, end: start + HOUR, rxBytes: at(0), txBytes: at(1), rxPeakBps: at(2), txPeakBps: at(3), current, future })
    }
    return [inst, period === 'week' ? toDays(hours, from) : hours]
  }))
}

/**
 * Map exit tags → MGMT IPs. Источник истины — runtimeConfig.mgmtIps (env
 * `NUXT_MGMT_IPS`), формат "tag:ip,tag:ip,...", приходит из 30-frontend.sh
 * со СТАБИЛЬНЫМИ индексами из config.yaml (исключённые экзиты резервируют
 * свой индекс, не сдвигая соседей). Это критично: wgmgmt-туннель на ноде
 * физически прописан на конкретный 10.99.0.X — если посчитать индекс
 * динамически (i+2 от текущей exitTags), при exclusion одного экзита
 * остальные «съезжают» в коде, но не на нодах, и scrape промахивается.
 *
 * Fallback (для совместимости со старыми деплоями без NUXT_MGMT_IPS):
 * считаем индексы из exitTags по позиции — старое поведение.
 */
export function nodeInstances(): Array<{ tag: string, instance: string, label: string }> {
  const cfg = useRuntimeConfig()
  const mgmtIpsRaw = String(cfg.mgmtIps || '').trim()
  const tags = String(cfg.exitTags || '').trim().split(/\s+/).filter(Boolean)

  if (mgmtIpsRaw) {
    const ipByTag = new Map<string, string>()
    for (const pair of mgmtIpsRaw.split(',')) {
      const [t, ip] = pair.split(':').map(s => s.trim())
      if (t && ip) ipByTag.set(t, ip)
    }
    const ruIp = ipByTag.get('ru') ?? '10.99.0.1'
    const nodes: Array<{ tag: string, instance: string, label: string }> = [
      { tag: 'ru', instance: `${ruIp}:9100`, label: 'RU entry' },
    ]
    for (const tag of tags) {
      const ip = ipByTag.get(tag)
      if (!ip) continue // фильтруем неизвестные теги — exclusion обрабатывается через config2env, тег исчезнет из exitTags
      nodes.push({ tag, instance: `${ip}:9100`, label: `${tag.toUpperCase()} exit` })
    }
    return nodes
  }

  // Fallback (legacy): динамическая нумерация. Сломается при exclusion.
  const prefix = cfg.mgmtMeshIpPrefix as string
  const nodes: Array<{ tag: string, instance: string, label: string }> = [
    { tag: 'ru', instance: `${prefix}1:9100`, label: 'RU entry' },
  ]
  tags.forEach((tag, i) => {
    nodes.push({
      tag,
      instance: `${prefix}${i + 2}:9100`,
      label: `${tag.toUpperCase()} exit`,
    })
  })
  return nodes
}
