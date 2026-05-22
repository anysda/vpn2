/**
 * Per-client OpenVPN helpers — kept separate from useClients so its
 * polling lifecycle is independent of the client list.
 */
export function useClientsOvpn() {
  async function getOvpnConfig(id: number) {
    return $fetch<string>(`/api/clients/${id}/ovpn-config`, { responseType: 'text' })
  }
  async function sendOvpnToTg(id: number) {
    return $fetch<{ ok: true }>(`/api/clients/${id}/send-ovpn-to-tg`, { method: 'POST' })
  }
  return { getOvpnConfig, sendOvpnToTg }
}
