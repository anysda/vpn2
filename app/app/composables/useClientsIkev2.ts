export interface Ikev2Credentials {
  server: string
  remoteId: string
  username: string
  password: string
  caCertPem: string | null
}

/**
 * Девайс-скоупные IKEv2-хелперы — server/username/password + CA + .mobileconfig.
 * Жизненный цикл независимый от useClients, как и для OVPN/WG.
 */
export function useClientsIkev2() {
  async function getIkev2Credentials(clientId: number, deviceId: number) {
    return $fetch<Ikev2Credentials>(
      `/api/clients/${clientId}/devices/${deviceId}/ikev2-credentials`,
    )
  }
  function ikev2MobileconfigUrl(clientId: number, deviceId: number) {
    return `/api/clients/${clientId}/devices/${deviceId}/ikev2-mobileconfig`
  }
  function ikev2CaCrtUrl() {
    return '/api/ikev2/ca.crt'
  }
  async function sendIkev2ToTg(clientId: number, deviceId: number, platform: 'ios' | 'android' | 'windows') {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}/send-ikev2-to-tg`,
      { method: 'POST', body: { platform } },
    )
  }
  return { getIkev2Credentials, ikev2MobileconfigUrl, ikev2CaCrtUrl, sendIkev2ToTg }
}
