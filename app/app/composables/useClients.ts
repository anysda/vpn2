import type { AccessOperation } from '#shared/access-operation'

/**
 * Модель «клиент = человек, девайс = устройство».
 * Клиент — человек со сроком действия, статусом, лимитом девайсов и паролем.
 * Девайсы клиента живут отдельно (см. ClientDetail.devices).
 */

/** Клиент в списке `GET /api/clients`. */
export interface Client {
  accessStatus: string
  id: number
  name: string
  filterTraffic: boolean
  expiresAt: string | null
  deviceLimit: number | null
  frozenManual: boolean
  createdAt: string
  status: 'active' | 'frozen'
  tgLinked: boolean
  tgUsername: string | null
  /** Предпочитаемый экзит (тег узла) или null — авто. */
  preferredExit: string | null
  deviceCount: number
  rxTotal: number
  txTotal: number
}

/** Девайс внутри детальной карточки клиента. */
export interface Device {
  id: number
  name: string
  /** Имя файла конфига: до 15 ASCII-символов, с уникальным суффиксом устройства. */
  configName: string
  createdAt: string
  rxTotal: number
  txTotal: number
  hasWg: boolean
  hasOvpn: boolean
}

/** Полная карточка клиента `GET /api/clients/:id`. */
export interface ClientDetail {
  operation: AccessOperation | null
  deleting: boolean
  id: number
  name: string
  filterTraffic: boolean
  expiresAt: string | null
  deviceLimit: number | null
  frozenManual: boolean
  createdAt: string
  status: 'active' | 'frozen'
  tgLinked: boolean
  tgUsername: string | null
  preferredExit: string | null
  rxTotal: number
  txTotal: number
  devices: Device[]
}

const POLL_INTERVAL_MS = 3000
const CLIENTS_KEY = 'clients'

export function useClients() {
  // Опрос и повторные загрузки не обрывают запрос в пути, а ждут его ответа:
  // иначе ответ дольше такта опроса не доходил до экрана никогда.
  const { data, refresh: poll, status, error } = useFetch<Client[]>('/api/clients', {
    key: CLIENTS_KEY,
    default: () => [],
    server: false,
    dedupe: 'defer',
  })
  useSharedPoll(CLIENTS_KEY, POLL_INTERVAL_MS)
  useVisibleRefresh(poll)
  /** После своей правки нужен свежий список, а не ответ, начатый до неё. */
  const refresh = () => poll({ dedupe: 'cancel' })

  async function create(payload: {
    name: string
    filterTraffic?: boolean
    expiresAt?: string | null
    deviceLimit?: number | null
  }) {
    const created = await $fetch<Client & { warning?: string }>('/api/clients', { method: 'POST', body: payload })
    await refresh()
    return created
  }

  async function update(id: number, patch: {
    name?: string
    expiresAt?: string | null
    deviceLimit?: number | null
    frozenManual?: boolean
    preferredExit?: string | null
  }) {
    const updated = await $fetch<Client & { warning?: string }>(`/api/clients/${id}`, { method: 'PATCH', body: patch, headers: { 'Idempotency-Key': crypto.randomUUID() } })
    await refresh()
    return updated
  }

  async function remove(id: number) {
    const result = await $fetch<{ operation: AccessOperation }>(`/api/clients/${id}`, { method: 'DELETE', headers: { 'Idempotency-Key': crypto.randomUUID() } })
    await refresh()
    return result
  }

  async function reissueAll(id: number) {
    const res = await $fetch<{ operation: AccessOperation }>(`/api/clients/${id}/reissue`, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } })
    return res
  }

  async function sendPasswordToTg(id: number) {
    return $fetch<{ ok: true }>(`/api/clients/${id}/send-password-to-tg`, { method: 'POST' })
  }

  async function unlink(id: number) {
    const result = await $fetch<{ ok: true, warning?: string }>(`/api/clients/${id}/unlink`, { method: 'POST' })
    await refresh()
    return result
  }

  return { clients: data, refresh, status, error, create, update, remove, reissueAll, sendPasswordToTg, unlink }
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
    return $fetch<{ id: number, clientId: number, name: string, createdAt: string, warning?: string }>(
      `/api/clients/${clientId}/devices`,
      { method: 'POST', body: { name } },
    )
  }

  async function removeDevice(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}`,
      { method: 'DELETE', headers: { 'Idempotency-Key': crypto.randomUUID() } },
    )
  }

  async function reissueDevice(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}/reissue`,
      { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } },
    )
  }

  return { addDevice, removeDevice, reissueDevice }
}

/** Трафик клиента из `GET /api/clients/traffic` (опрос каждые 3 с). */
export interface ClientTraffic {
  /** За всё время. */
  rxBytes: number
  txBytes: number
  /** С 00:00 МСК. */
  dayRx: number
  dayTx: number
  /** За последние 5 минут. */
  recentBytes: number
  /** «В сети»: recentBytes выше порога keepalive-шума. */
  online: boolean
}

/** «31.12.2026» → ISO-строка (UTC-полночь). null — если формат не распознан. */
export function parseRuDate(s: string): string | null {
  const m = s.trim().match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/)
  if (!m) return null
  const dd = Number(m[1])
  const mm = Number(m[2])
  const yyyy = Number(m[3])
  const d = new Date(Date.UTC(yyyy, mm - 1, dd))
  // Отсеять несуществующие даты (напр. 31.02.2026).
  if (d.getUTCDate() !== dd || d.getUTCMonth() !== mm - 1) return null
  return d.toISOString()
}

/** ISO-строка → «31.12.2026» (пусто, если даты нет). */
export function formatRuDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`
}

export function expiryLabel(expiresAt: string | null | undefined): string {
  if (!expiresAt) return 'Бессрочный'
  const d = new Date(expiresAt)
  return `до ${d.toLocaleDateString('ru-RU')}`
}

/** Человекочитаемый размер в байтах. */
