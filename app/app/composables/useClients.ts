import type { AccessOperation } from '#shared/access-operation'

/** Клиент - человек со сроком, статусом и лимитом; девайсы - его устройства. */
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
  /** Предпочитаемый экзит (тег узла) или null - авто. */
  preferredExit: string | null
  deviceCount: number
  rxTotal: number
  txTotal: number
}

export interface Device {
  id: number
  name: string
  /** Имя файла конфига: до 15 ASCII-символов с уникальным суффиксом. */
  configName: string
  createdAt: string
  rxTotal: number
  txTotal: number
}

export type ClientDetail = Omit<Client, 'accessStatus' | 'deviceCount'> & {
  operation: AccessOperation | null
  deleting: boolean
  devices: Device[]
}

/** Трафик клиента: за всё время, с 00:00 МСК, за 5 минут; online - выше шума keepalive. */
export interface ClientTraffic { rxBytes: number, txBytes: number, dayRx: number, dayTx: number, recentBytes: number, online: boolean }

/** Список клиентов раз в 3 с; сбой опроса оставляет последний удачный список. */
export function useClients() {
  const last = useState<Client[]>('clients-last', () => [])
  const { data, refresh, status, error } = useFetch<Client[]>('/api/clients', {
    key: 'clients',
    default: () => last.value,
    transform: list => (last.value = list),
    server: false,
    dedupe: 'defer',
    timeout: 10_000,
  })
  usePoll(refresh, 3000)
  return { clients: data, refresh: () => refresh({ dedupe: 'cancel' }), status, error }
}

type Warned = { warning?: string }
type Method = 'POST' | 'PATCH' | 'DELETE'
/** Правка клиента; `once` - с ключом запроса: повтор сервер узнаёт и не выполняет. */
const send = <T>(path: string, method: Method, body?: object, once = false) =>
  $fetch(`/api/clients${path}`, { method, body, headers: once ? { 'Idempotency-Key': crypto.randomUUID() } : undefined }) as Promise<T>

export const fetchClient = (id: number) => $fetch<ClientDetail>(`/api/clients/${id}`)
export const createClient = (c: { name: string, filterTraffic: boolean, expiresAt: string | null, deviceLimit: number | null }) =>
  send<Client & Warned>('', 'POST', c)
type Patch = Partial<Pick<Client, 'name' | 'expiresAt' | 'deviceLimit' | 'frozenManual' | 'preferredExit'>>
export const updateClient = (id: number, patch: Patch) => send<Warned>(`/${id}`, 'PATCH', patch, true)
export const removeClient = (id: number) => send<{ operation: AccessOperation }>(`/${id}`, 'DELETE', undefined, true)
export const reissueClient = (id: number) => send(`/${id}/reissue`, 'POST', undefined, true)
export const sendBotAccess = (id: number) => send(`/${id}/send-password-to-tg`, 'POST')
export const unlinkClient = (id: number) => send<Warned>(`/${id}/unlink`, 'POST')
export const addDevice = (id: number, name: string) => send<Warned>(`/${id}/devices`, 'POST', { name })
export const removeDevice = (id: number, device: number) => send(`/${id}/devices/${device}`, 'DELETE', undefined, true)
export const reissueDevice = (id: number, device: number) => send(`/${id}/devices/${device}/reissue`, 'POST', undefined, true)
