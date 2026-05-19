export interface Route {
  id: number
  type: 'domain' | 'ip_cidr'
  value: string
  outbound: string
  createdAt: string
}

export interface Outbound {
  name: string
  type: string
  delay: number | null
  isWarp: boolean
  isDirectRu: boolean
}

export function useRoutes() {
  const { data: rules, refresh: refreshRules } = useFetch<Route[]>('/api/routes', {
    default: () => [],
    server: false,
  })
  const { data: outbounds, refresh: refreshOutbounds } = useFetch<Outbound[]>('/api/routes/outbounds', {
    default: () => [],
    server: false,
  })

  let timer: ReturnType<typeof setInterval> | null = null

  onMounted(() => {
    if (!timer) {
      timer = setInterval(() => {
        void refreshRules()
        void refreshOutbounds()
      }, 4000)
    }
  })

  onUnmounted(() => {
    if (timer) { clearInterval(timer); timer = null }
  })

  useVisibleRefresh(() => {
    void refreshRules()
    void refreshOutbounds()
  })

  async function create(value: string, outbound: string) {
    const r = await $fetch<Route>('/api/routes', { method: 'POST', body: { value, outbound } })
    await refreshRules()
    return r
  }

  async function patch(id: number, outbound: string) {
    const r = await $fetch<Route>(`/api/routes/${id}`, { method: 'PATCH', body: { outbound } })
    await refreshRules()
    return r
  }

  async function remove(id: number) {
    await $fetch(`/api/routes/${id}`, { method: 'DELETE' })
    await refreshRules()
  }

  return { rules, outbounds, refreshRules, refreshOutbounds, create, patch, remove }
}

export function rttColor(delay: number | null): 'success' | 'warning' | 'error' | 'neutral' {
  if (delay === null || delay <= 0) return 'error'
  if (delay < 100) return 'success'
  if (delay < 300) return 'warning'
  return 'error'
}

export function flagFor(tag: string): string {
  const flags: Record<string, string> = {
    ru: '🇷🇺',
    us: '🇺🇸',
    gb: '🇬🇧',
    de: '🇩🇪',
    nl: '🇳🇱',
    se: '🇸🇪',
    fr: '🇫🇷',
    fi: '🇫🇮',
    pl: '🇵🇱',
  }
  return flags[tag] ?? '🌍'
}

/** Russian plural: pluralRu(1, ['правило','правила','правил']) → 'правило' */
export function pluralRu(n: number, forms: [string, string, string]): string {
  const abs = Math.abs(n)
  const mod10 = abs % 10
  const mod100 = abs % 100
  if (mod10 === 1 && mod100 !== 11) return forms[0]
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return forms[1]
  return forms[2]
}

/** "hy2-us-direct" → { tag: "us", variant: "direct" }, "direct-ru" → { tag: "ru", variant: "direct" } */
export function parseOutbound(name: string): { tag: string, variant: 'direct' | 'warp' } | null {
  if (name === 'direct-ru') return { tag: 'ru', variant: 'direct' }
  const m = name.match(/^hy2-([a-z]+)-(direct|warp)$/)
  if (!m) return null
  return { tag: m[1]!, variant: m[2] as 'direct' | 'warp' }
}
