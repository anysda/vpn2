import type { AccessOperation } from '#shared/access-operation'

export type Client = Api<'/api/clients'>[number]
export type ClientDetail = Api<'/api/clients/:id'>
export type Device = ClientDetail['devices'][number]
/** Трафик клиента: за всё время, с 00:00 МСК, за 5 минут; online - выше шума keepalive. */
export type ClientTraffic = Api<'/api/clients/traffic'>[number]

/** Список клиентов раз в 3 с. */
export function useClients() {
  const { data, refresh, status, error } = usePolledFetch('/api/clients', 3000, 10_000, [] as Client[])
  return { clients: data, refresh: () => refresh({ dedupe: 'cancel' }), status, error }
}

type Warned = { warning?: string }
type Method = 'POST' | 'PATCH' | 'DELETE'
/** Правка клиента; `once` - с ключом запроса: повтор сервер узнаёт и не выполняет. */
function send<T>(path: string, method: Method, body?: object, once = false): Promise<T> {
  const headers: HeadersInit | undefined = once ? { 'Idempotency-Key': crypto.randomUUID() } : undefined
  return $fetch<unknown>(`/api/clients${path}`, { method, body, headers }) as Promise<T>
}

export const fetchClient = (id: number) => $fetch(`/api/clients/${id}`)
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
