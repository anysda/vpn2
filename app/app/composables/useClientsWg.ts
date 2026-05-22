/**
 * Per-client WireGuard helpers — separate from useClients so its polling
 * lifecycle is independent of the client list.
 */
export function useClientsWg() {
  async function getWgConfig(id: number) {
    return $fetch<string>(`/api/clients/${id}/wg-config`, { responseType: 'text' })
  }
  async function sendWgToTg(id: number) {
    return $fetch<{ ok: true }>(`/api/clients/${id}/send-wg-to-tg`, { method: 'POST' })
  }
  return { getWgConfig, sendWgToTg }
}
