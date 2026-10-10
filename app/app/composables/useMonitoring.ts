import { useTrafficPeriod } from '~/composables/useClients'
export interface NodeMetric {
  tag: string
  label: string
  // публичный адрес ноды (из config.yaml), null — не задан
  host: string | null
  instance: string
  cpu: number | null
  // ожидание диска и украденное гипервизором время, % (в cpu не входят)
  iowait: number | null
  steal: number | null
  ram: number | null
  rxMbps: number | null
  txMbps: number | null
  uptimeSec: number | null
  // свободно на корневой ФС: байты и % от размера
  diskFreeBytes: number | null
  diskFreePct: number | null
  // возраст последних метрик ноды, сек (null — данных нет)
  staleSec: number | null
  // байты через WAN ноды за выбранный период (сутки / вчера / неделя), вход / выход
  trafficPeriod: 'day' | 'yesterday' | 'week'
  rxPeriodBytes: number | null
  txPeriodBytes: number | null
  // экзит выключен вручную из панели (watchdog обходит его как мёртвый)
  disabled: boolean
  // через этот экзит сейчас идёт иностранный трафик вне дорожек (foreign-best)
  active: boolean
  // клиентских устройств с соединениями через экзит (null у RU)
  activeDevices: number | null
  // дорожек lane-NN, направленных на узел, и сколько из них через его warp
  // (null у RU и когда дорожек в конфиге нет)
  lanes: number | null
  lanesWarp: number | null
}

export type NodeState = 'online' | 'warning' | 'offline'

// Метрики скрейпятся раз в 2с (VM latencyOffset 1с) — здоровая нода держит
// staleSec ~0-3с. warning: метрики не обновляются ≥5с — нода потеряла связь
//          (трафик уже увёл watchdog) → красим карточку красным почти сразу.
// offline: ≥3 мин без метрик.
const WARN_AFTER_SEC = 5
const OFFLINE_AFTER_SEC = 180

export function nodeState(staleSec: number | null | undefined): NodeState {
  if (staleSec == null || staleSec >= OFFLINE_AFTER_SEC) return 'offline'
  if (staleSec >= WARN_AFTER_SEC) return 'warning'
  return 'online'
}

// Трафик ноды по корзинам периода — диаграмма в карточке: сутки и вчера —
// по часам, неделя — по суткам МСК.
export interface TrafficBucket {
  // unix-секунды границ корзины [start, end)
  start: number
  end: number
  // null — данных нет (не ноль)
  rxBytes: number | null
  txBytes: number | null
  rxPeakBps: number | null
  txPeakBps: number | null
  // текущая, ещё не закончившаяся корзина
  current: boolean
  // ещё не наступила (часы суток после текущего)
  future: boolean
}

export type BucketUnit = 'hour' | 'day'

const HOURLY_REFRESH_MS = 5000

export function useMonitoring() {
  // Период трафика нод — общий с селектором ТОП клиентов.
  const period = useTrafficPeriod()
  const { data: nodes, refresh: refreshNodes } = useFetch<NodeMetric[]>('/api/ops/nodes', {
    query: { period },
    default: () => [],
    server: false,
  })

  // Диаграмма трафика по тому же периоду: отдельный эндпоинт и свой, более
  // редкий опрос — текущий час на сервере кэшируется на 5с, полные часы — до
  // смены часа. Смена периода перезапрашивает сразу (query реактивный).
  const { data: bucketsResp, refresh: refreshHourly } = useFetch<{ period: string, unit: BucketUnit, nodes: Array<{ tag: string, buckets: TrafficBucket[] }> }>('/api/ops/nodes/hourly', {
    query: { period },
    server: false,
  })
  // Пока ответ не совпал с выбранным периодом — не показываем старую диаграмму под новой подписью.
  const hourly = computed(() => (bucketsResp.value?.period === period.value
    ? new Map(bucketsResp.value.nodes.map(n => [n.tag, n.buckets]))
    : new Map<string, TrafficBucket[]>()))
  const bucketUnit = computed<BucketUnit>(() => bucketsResp.value?.unit ?? 'hour')

  let timer: ReturnType<typeof setInterval> | null = null
  let hourlyTimer: ReturnType<typeof setInterval> | null = null
  onMounted(() => {
    if (!timer) {
      timer = setInterval(() => {
        void refreshNodes()
      }, 2000)
    }
    if (!hourlyTimer) {
      hourlyTimer = setInterval(() => {
        void refreshHourly()
      }, HOURLY_REFRESH_MS)
    }
  })
  onUnmounted(() => {
    if (timer) { clearInterval(timer); timer = null }
    if (hourlyTimer) { clearInterval(hourlyTimer); hourlyTimer = null }
  })

  useVisibleRefresh(() => {
    void refreshNodes()
    void refreshHourly()
  })

  async function setExitDisabled(tag: string, disabled: boolean) {
    await $fetch(`/api/ops/exits/${encodeURIComponent(tag)}`, {
      method: 'PATCH',
      body: { disabled },
    })
    await refreshNodes()
  }

  return { nodes, hourly, bucketUnit, refreshNodes, setExitDisabled }
}

export function formatUptime(sec: number | null | undefined): string {
  if (!sec || sec < 0) return '—'
  if (sec < 60) return `${Math.floor(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
  const days = Math.floor(sec / 86400)
  const hours = Math.floor((sec % 86400) / 3600)
  return `${days}d ${hours}h`
}

export function formatPercent(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v.toFixed(1)}%`
}

export function formatMbps(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v < 0.01) return '0'
  return v.toFixed(2)
}

export function formatBytes(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v < 1e9) return `${(v / 1e6).toFixed(0)} MB`
  return `${(v / 1e9).toFixed(2)} GB`
}
