export interface Ikev2Credentials {
  server: string
  remoteId: string
  username: string
  password: string
  caCertPem: string | null
}

/**
 * Девайс-скоупные IKEv2-хелперы — server/username/password + CA скачать +
 * отправить в TG (два сообщения: креды + ca.crt). Жизненный цикл
 * независимый от useClients, как и для OVPN/WG.
 */
export function useClientsIkev2() {
  async function getIkev2Credentials(clientId: number, deviceId: number) {
    return $fetch<Ikev2Credentials>(
      `/api/clients/${clientId}/devices/${deviceId}/ikev2-credentials`,
    )
  }
  function ikev2CaCrtUrl() {
    return '/api/ikev2/ca.crt'
  }
  async function sendIkev2ToTg(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}/send-ikev2-to-tg`,
      { method: 'POST' },
    )
  }
  return { getIkev2Credentials, ikev2CaCrtUrl, sendIkev2ToTg }
}
