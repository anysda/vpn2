export interface NodeMetric {
  tag: string
  label: string
  instance: string
  cpu: number | null
  ram: number | null
  rxMbps: number | null
  txMbps: number | null
  uptimeSec: number | null
  // возраст последних метрик ноды, сек (null — данных нет)
  staleSec: number | null
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

export interface OutboundMetric {
  name: string
  rttMs: number | null
  status: 'up' | 'down' | 'warn'
  upKbps: number
  downKbps: number
  isWarp: boolean
}

const HISTORY_LEN = 60

export function useMonitoring() {
  const { data: nodes, refresh: refreshNodes } = useFetch<NodeMetric[]>('/api/ops/nodes', {
    default: () => [],
    server: false,
  })
  const { data: outbounds, refresh: refreshOutbounds } = useFetch<OutboundMetric[]>('/api/ops/outbounds', {
    default: () => [],
    server: false,
  })

  // sparkline history per node CPU
  const cpuHistory = ref<Map<string, number[]>>(new Map())

  function pushHistory(tag: string, cpu: number | null) {
    const h = cpuHistory.value.get(tag) ?? []
    h.push(cpu ?? 0)
    while (h.length > HISTORY_LEN) h.shift()
    cpuHistory.value.set(tag, h)
  }

  watch(nodes, (list) => {
    list.forEach(n => pushHistory(n.tag, n.cpu))
  }, { deep: true })

  let timer: ReturnType<typeof setInterval> | null = null
  onMounted(() => {
    if (!timer) {
      timer = setInterval(() => {
        void refreshNodes()
        void refreshOutbounds()
      }, 2000)
    }
  })
  onUnmounted(() => { if (timer) { clearInterval(timer); timer = null } })

  useVisibleRefresh(() => {
    void refreshNodes()
    void refreshOutbounds()
  })

  return { nodes, outbounds, cpuHistory, refreshNodes, refreshOutbounds }
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

export function sparklinePath(values: number[], width = 80, height = 24): string {
  if (values.length === 0) return ''
  const max = Math.max(...values, 1)
  const step = width / Math.max(values.length - 1, 1)
  return values
    .map((v, i) => {
      const x = i * step
      const y = height - (v / max) * height
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}
