/**
 * Девайс-скоупные WireGuard-хелперы — отдельно от useClients, чтобы их
 * жизненный цикл не зависел от списка клиентов.
 * Ключи WG принадлежат девайсу, поэтому все URL — `/api/clients/:id/devices/:deviceId/...`.
 */
export function useClientsWg() {
  async function getWgConfig(clientId: number, deviceId: number) {
    return $fetch<string>(
      `/api/clients/${clientId}/devices/${deviceId}/wg-config`,
      { responseType: 'text' },
    )
  }
  async function sendWgToTg(clientId: number, deviceId: number) {
    return $fetch<{ ok: true }>(
      `/api/clients/${clientId}/devices/${deviceId}/send-wg-to-tg`,
      { method: 'POST' },
    )
  }
  function wgQrUrl(clientId: number, deviceId: number) {
    return `/api/clients/${clientId}/devices/${deviceId}/wg-qrcode.svg`
  }
  return { getWgConfig, sendWgToTg, wgQrUrl }
}
