/**
 * DNS servers handed to a client based on its filterTraffic flag.
 *  - filter ON  → AdGuard Home on the entry mgmt IP (ad/tracker filtering)
 *  - filter OFF → plain public resolvers
 * Used by the WireGuard .conf and OpenVPN .ovpn builders.
 */
export function clientDns(filterTraffic: boolean): string[] {
  return filterTraffic ? ['10.99.0.1'] : ['8.8.8.8', '1.1.1.1']
}
