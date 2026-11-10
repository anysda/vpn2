export type ConfigKind = 'wg' | 'ovpn' | 'ike'

interface Ikev2Credentials {
  server: string
  remoteId: string
  username: string
  password: string
  caCertPem: string | null
  certificatePending: boolean
}

type AccessConfig<K extends ConfigKind> = K extends 'ike' ? Ikev2Credentials : string

const ROUTES: Record<ConfigKind, { config: string, send: string }> = {
  wg: { config: 'wg-config', send: 'send-wg-to-tg' },
  ovpn: { config: 'ovpn-config', send: 'send-ovpn-to-tg' },
  ike: { config: 'ikev2-credentials', send: 'send-ikev2-to-tg' },
}

/**
 * Доступ устройства по одному протоколу: конфиг (текст у WireGuard и OpenVPN,
 * креды у IKEv2), отправка в Telegram и постоянные адреса файлов.
 * Ключи принадлежат устройству, поэтому адреса вида
 * `/api/clients/:id/devices/:deviceId/...`; жизненный цикл не зависит от
 * списка клиентов.
 */
export function useDeviceAccess<K extends ConfigKind>(kind: K) {
  const base = (clientId: number, deviceId: number) => `/api/clients/${clientId}/devices/${deviceId}`

  async function getConfig(clientId: number, deviceId: number): Promise<AccessConfig<K>> {
    const url = `${base(clientId, deviceId)}/${ROUTES[kind].config}`
    if (kind === 'ike') return await $fetch<Ikev2Credentials>(url) as AccessConfig<K>
    return await $fetch<string>(url, { responseType: 'text' }) as AccessConfig<K>
  }
  async function sendToTg(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(`${base(clientId, deviceId)}/${ROUTES[kind].send}`, { method: 'POST' })
  }
  /** QR есть только у WireGuard. */
  function qrUrl(clientId: number, deviceId: number) {
    return `${base(clientId, deviceId)}/wg-qrcode.svg`
  }
  /** Корневой сертификат один на сервер, нужен только IKEv2. */
  function caCrtUrl() {
    return '/api/ikev2/ca.crt'
  }
  return { getConfig, sendToTg, qrUrl, caCrtUrl }
}
