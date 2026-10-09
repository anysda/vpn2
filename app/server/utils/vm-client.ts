import { useLogger } from './logger'
import { MSK_OFFSET_SEC, mskDay } from './msk-day'

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

// Суточный трафик ноды: increase() WAN-счётчиков с 00:00 МСК. Фильтр строже,
// чем у rx/tx Mbps: на entry трафик клиента виден и на wg0/tun0/xfrm0, и на
// eth0 — считаем только физический интерфейс, иначе сумма удвоится.
const DAILY_DEVICE_FILTER = 'device!~"lo|wg.*|tun.*|xfrm.*|docker.*|veth.*|br-.*"'
const DAILY_CACHE_MS = 30_000

/** Секунд с 00:00 по Москве; не меньше 60, чтобы окно increase() не было пустым. */
function secondsSinceMskMidnight(nowMs = Date.now()): number {
  const sec = Math.floor(nowMs / 1000)
  return Math.max(60, (sec + MSK_OFFSET_SEC) % 86400)
}

export interface DailyTraffic {
  rxBytes: number | null
  txBytes: number | null
}

// increase() за сутки на 2с-скрейпе — тяжёлый запрос; панель опрашивает
// /api/ops/nodes раз в 2с, поэтому результат держим 30с.
let dailyCache: { at: number, day: number, rx: Map<string, number>, tx: Map<string, number> } | null = null


export async function fetchDailyTraffic(instances: string[]): Promise<Map<string, DailyTraffic>> {
  const now = Date.now()
  const day = mskDay(now)
  if (dailyCache && dailyCache.day !== day) {
    // Полночь МСК: ни кэш, ни holdover не должны показывать вчерашние сутки.
    dailyCache = null
    for (const m of holdover.values()) { m.delete('rxDay'); m.delete('txDay') }
  }
  if (!dailyCache || now - dailyCache.at > DAILY_CACHE_MS) {
    const range = `${secondsSinceMskMidnight(now)}s`
    const [rx, tx] = await Promise.all([
      instantQuery(`sum by(instance) (increase(node_network_receive_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]))`),
      instantQuery(`sum by(instance) (increase(node_network_transmit_bytes_total{${DAILY_DEVICE_FILTER}}[${range}]))`),
    ])
    dailyCache = { at: now, day, rx, tx }
  }
  const rxH = withHoldover(dailyCache.rx, instances, 'rxDay')
  const txH = withHoldover(dailyCache.tx, instances, 'txDay')
  return new Map(instances.map(inst => [inst, {
    rxBytes: rxH.get(inst) ?? null,
    txBytes: txH.get(inst) ?? null,
  }]))
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
