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
export type ClientDetail = Omit<Client, 'accessStatus' | 'deviceCount'> & {
  operation: AccessOperation | null
  deleting: boolean
  devices: Device[]
}

const POLL_INTERVAL_MS = 3000
const CLIENTS_KEY = 'clients'
/** Дольше такта опроса: медленный ответ доходит, зависший не держит опрос вечно. */
export const POLL_TIMEOUT_MS = 10_000

export function useClients() {
  // Ошибка запроса сбрасывает data в default(): default отдаёт последний
  // удачный список, и сбой одного тика не рисует ложную пустоту.
  const last = useState<Client[]>('clients-last', () => [])
  // Опрос и повторные загрузки не обрывают запрос в пути, а ждут его ответа:
  // иначе ответ дольше такта опроса не доходил до экрана никогда. Зависший
  // запрос через POLL_TIMEOUT_MS становится ошибкой, и следующий тик повторяет.
  const { data, refresh: poll, status, error } = useFetch<Client[]>('/api/clients', {
    key: CLIENTS_KEY,
    default: () => last.value,
    transform: list => (last.value = list),
    server: false,
    dedupe: 'defer',
    timeout: POLL_TIMEOUT_MS,
  })
  useSharedPoll(CLIENTS_KEY, POLL_INTERVAL_MS)
  useVisibleRefresh(poll)
  /** После своей правки нужен свежий список, а не ответ, начатый до неё. */
  const refresh = () => poll({ dedupe: 'cancel' })
  return { clients: data, refresh, status, error }
}

type Warned = { warning?: string }
type Accepted = { operation: AccessOperation }
/** Правка клиента или девайса. */
function send<T>(path: string, method: 'POST' | 'PATCH' | 'DELETE', body?: object, headers?: HeadersInit) {
  return $fetch<unknown>(`/api/clients${path}`, { method, body, headers }) as Promise<T>
}
/** То же с ключом запроса: повтор сервер узнаёт и второй раз не выполняет. */
const sendOnce = <T>(path: string, method: 'POST' | 'PATCH' | 'DELETE', body?: object) =>
  send<T>(path, method, body, { 'Idempotency-Key': crypto.randomUUID() })

export const fetchClient = (id: number) => $fetch<ClientDetail>(`/api/clients/${id}`)
export const createClient = (c: { name: string, filterTraffic: boolean, expiresAt: string | null, deviceLimit: number | null }) =>
  send<Client & Warned>('', 'POST', c)
type Patch = Partial<Pick<Client, 'name' | 'expiresAt' | 'deviceLimit' | 'frozenManual' | 'preferredExit'>>
export const updateClient = (id: number, patch: Patch) => sendOnce<Warned>(`/${id}`, 'PATCH', patch)
export const removeClient = (id: number) => sendOnce<Accepted>(`/${id}`, 'DELETE')
export const reissueClient = (id: number) => sendOnce<Accepted>(`/${id}/reissue`, 'POST')
export const sendBotAccess = (id: number) => send<unknown>(`/${id}/send-password-to-tg`, 'POST')
export const unlinkClient = (id: number) => send<Warned>(`/${id}/unlink`, 'POST')
export const addDevice = (id: number, name: string) => send<Warned>(`/${id}/devices`, 'POST', { name })
export const removeDevice = (id: number, device: number) => sendOnce<unknown>(`/${id}/devices/${device}`, 'DELETE')
export const reissueDevice = (id: number, device: number) => sendOnce<unknown>(`/${id}/devices/${device}/reissue`, 'POST')

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
