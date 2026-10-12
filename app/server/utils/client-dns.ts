/**
 * DNS servers handed to a client based on its filterTraffic flag.
 *  - filter ON  → AdGuard Home on the entry mgmt IP (ad/tracker filtering)
 *  - filter OFF → адреса, перехватываемые TPROXY на хабе и передаваемые
 *    отдельному AdGuard без фильтров (то же разделение ru / остальные).
 *    Сохраняем адреса для совместимости с уже выданными конфигами.
 * Used by WireGuard, OpenVPN and the per-device IKEv2 pools.
 */
export function clientDns(filterTraffic: boolean): string[] {
  return filterTraffic ? ['10.99.0.1'] : ['8.8.8.8', '1.1.1.1']
}
