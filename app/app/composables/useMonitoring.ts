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

export function useMonitoring() {
  const { data: nodes, refresh: refreshNodes } = useFetch<NodeMetric[]>('/api/ops/nodes', {
    default: () => [],
    server: false,
  })

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

  return { nodes, refreshNodes, setExitDisabled }
}
