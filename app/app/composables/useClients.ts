/**
 * Модель «клиент = человек, девайс = устройство».
 * Клиент — человек со сроком действия, статусом, лимитом девайсов и паролем.
 * Девайсы клиента живут отдельно (см. ClientDetail.devices).
 */

/** Клиент в списке `GET /api/clients`. */
export interface Client {
  id: number
  name: string
  filterTraffic: boolean
  expiresAt: string | null
  deviceLimit: number | null
  frozenManual: boolean
  createdAt: string
  status: 'active' | 'frozen'
  deviceCount: number
  rxTotal: number
  txTotal: number
}

/** Девайс внутри детальной карточки клиента. */
export interface Device {
  id: number
  name: string
  createdAt: string
  rxTotal: number
  txTotal: number
  hasWg: boolean
  hasOvpn: boolean
}

/** Полная карточка клиента `GET /api/clients/:id`. */
export interface ClientDetail {
  id: number
  name: string
  filterTraffic: boolean
  expiresAt: string | null
  deviceLimit: number | null
  frozenManual: boolean
  password: string
  createdAt: string
  status: 'active' | 'frozen'
  rxTotal: number
  txTotal: number
  devices: Device[]
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

  async function create(payload: { name: string, filterTraffic?: boolean }) {
    const created = await $fetch<Client>('/api/clients', { method: 'POST', body: payload })
    await refresh()
    return created
  }

  async function update(id: number, patch: {
    name?: string
    expiresAt?: string | null
    deviceLimit?: number | null
    frozenManual?: boolean
  }) {
    const updated = await $fetch<Client>(`/api/clients/${id}`, { method: 'PATCH', body: patch })
    await refresh()
    return updated
  }

  async function remove(id: number) {
    await $fetch(`/api/clients/${id}`, { method: 'DELETE' })
    await refresh()
  }

  async function reissueAll(id: number) {
    const res = await $fetch<{ ok: true, devices: number }>(`/api/clients/${id}/reissue`, { method: 'POST' })
    return res
  }

  async function sendPasswordToTg(id: number) {
    return $fetch<{ ok: true }>(`/api/clients/${id}/send-password-to-tg`, { method: 'POST' })
  }

  return { clients: data, refresh, status, error, create, update, remove, reissueAll, sendPasswordToTg }
}

/** Загрузка детальной карточки клиента (по требованию, без поллинга). */
export function useClientDetail() {
  function fetchDetail(id: number) {
    return $fetch<ClientDetail>(`/api/clients/${id}`)
  }
  return { fetchDetail }
}

/** Девайс-операции клиента. */
export function useClientDevices() {
  async function addDevice(clientId: number, name: string) {
    return $fetch<{ id: number, clientId: number, name: string, createdAt: string }>(
      `/api/clients/${clientId}/devices`,
      { method: 'POST', body: { name } },
    )
  }

  async function removeDevice(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}`,
      { method: 'DELETE' },
    )
  }

  async function reissueDevice(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}/reissue`,
      { method: 'POST' },
    )
  }

  return { addDevice, removeDevice, reissueDevice }
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

/** Человекочитаемый размер в байтах. */
export function fmtBytes(n: number | undefined | null): string {
  if (!n) return '0'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}
