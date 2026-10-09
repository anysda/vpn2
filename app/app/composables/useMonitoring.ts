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
  // байты через WAN ноды с 00:00 МСК (вход / выход)
  rxTodayBytes: number | null
  txTodayBytes: number | null
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

const HISTORY_LEN = 60

export function useMonitoring() {
  const { data: nodes, refresh: refreshNodes } = useFetch<NodeMetric[]>('/api/ops/nodes', {
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
      }, 2000)
    }
  })
  onUnmounted(() => { if (timer) { clearInterval(timer); timer = null } })

  useVisibleRefresh(() => {
    void refreshNodes()
  })

  async function setExitDisabled(tag: string, disabled: boolean) {
    await $fetch(`/api/ops/exits/${encodeURIComponent(tag)}`, {
      method: 'PATCH',
      body: { disabled },
    })
    await refreshNodes()
  }

  return { nodes, cpuHistory, refreshNodes, setExitDisabled }
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
