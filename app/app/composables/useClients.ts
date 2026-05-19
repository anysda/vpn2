export interface Client {
  id: number
  name: string
  ssSecret: string
  cipher: string
  enabled: boolean
  expiresAt: string | null
  createdAt: string
  updatedAt: string
}

const POLL_INTERVAL_MS = 3000

export function useClients() {
  const { data, refresh, status, error } = useFetch<Client[]>('/api/clients', {
    default: () => [],
    server: false,
  })

  let timer: ReturnType<typeof setInterval> | null = null

  onMounted(() => {
    if (!timer) timer = setInterval(() => { void refresh() }, POLL_INTERVAL_MS)
  })

  onUnmounted(() => {
    if (timer) { clearInterval(timer); timer = null }
  })

  useVisibleRefresh(refresh)

  async function create(payload: { name: string, expiresAt: string | null, sendToTg?: boolean }) {
    const created = await $fetch<Client>('/api/clients', { method: 'POST', body: payload })
    await refresh()
    return created
  }

  async function sendToTg(id: number) {
    return $fetch<{ ok: true }>(`/api/clients/${id}/send-to-tg`, { method: 'POST' })
  }

  async function update(id: number, patch: { name?: string, enabled?: boolean, expiresAt?: string | null }) {
    const updated = await $fetch<Client>(`/api/clients/${id}`, { method: 'PATCH', body: patch })
    await refresh()
    return updated
  }

  async function remove(id: number) {
    await $fetch(`/api/clients/${id}`, { method: 'DELETE' })
    await refresh()
  }

  async function getSsUrl(id: number) {
    return $fetch<string>(`/api/clients/${id}/ss-url`)
  }

  async function generateOneTimeLink(id: number) {
    return $fetch<{ token: string, url: string, expiresAt: string, ttlSeconds: number }>(
      `/api/clients/${id}/one-time-link`,
      { method: 'POST' },
    )
  }

  return { clients: data, refresh, status, error, create, update, remove, getSsUrl, generateOneTimeLink, sendToTg }
}

export function relativeTime(date: Date | string | null | undefined): string {
  if (!date) return ''
  const d = typeof date === 'string' ? new Date(date) : date
  const sec = Math.floor((Date.now() - d.getTime()) / 1000)
  if (sec < 0) return 'в будущем'
  if (sec < 60) return `${sec} сек назад`
  if (sec < 3600) return `${Math.floor(sec / 60)} мин назад`
  if (sec < 86400) return `${Math.floor(sec / 3600)} ч назад`
  return `${Math.floor(sec / 86400)} дн назад`
}

export function expiryLabel(expiresAt: string | null | undefined): string {
  if (!expiresAt) return 'Бессрочный'
  const d = new Date(expiresAt)
  return `до ${d.toLocaleDateString('ru-RU')}`
}
