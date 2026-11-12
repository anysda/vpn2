/**
 * DNS клиента во всех протоколах. С фильтрацией - AdGuard на mgmt-адресе entry; без неё - адреса,
 * которые TPROXY на хабе отдаёт AdGuard без фильтров: они уже вшиты в выданные конфиги.
 */
export function clientDns(filterTraffic: boolean): string[] {
  return filterTraffic ? ['10.99.0.1'] : ['8.8.8.8', '1.1.1.1']
}
