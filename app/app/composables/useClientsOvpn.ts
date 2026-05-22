/**
 * Девайс-скоупные OpenVPN-хелперы — отдельно от useClients, чтобы их
 * жизненный цикл не зависел от списка клиентов.
 * Ключи OVPN принадлежат девайсу, поэтому все URL — `/api/clients/:id/devices/:deviceId/...`.
 */
export function useClientsOvpn() {
  async function getOvpnConfig(clientId: number, deviceId: number) {
    return $fetch<string>(
      `/api/clients/${clientId}/devices/${deviceId}/ovpn-config`,
      { responseType: 'text' },
    )
  }
  async function sendOvpnToTg(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}/send-ovpn-to-tg`,
      { method: 'POST' },
    )
  }
  return { getOvpnConfig, sendOvpnToTg }
}
