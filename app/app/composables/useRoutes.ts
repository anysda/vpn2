import { parseOutbound } from '#shared/outbounds'

export { parseOutbound }

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

/** Ключ списка правил: заголовок раздела читает его без второго опроса. */
export const ROUTES_KEY = 'routes'

export function useRoutes() {
  const { data: rules, refresh: refreshRules } = useFetch<Route[]>('/api/routes', {
    key: ROUTES_KEY,
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

/** Экзиты для выпадашки «предпочитаемый экзит» в модалке клиента (direct-выходы, без RU). */
export function useExitTags() {
  const { data: outboundList } = useFetch<Outbound[]>('/api/routes/outbounds', {
    default: () => [],
    server: false,
  })
  return computed(() => [...new Set(
    outboundList.value
      .map(o => parseOutbound(o.name))
      .filter(p => p && p.variant === 'direct' && p.tag !== 'ru')
      .map(p => p!.tag),
  )].sort())
}
